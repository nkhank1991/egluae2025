import os, re, csv, json, time, html, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse
from urllib import robotparser
from collections import defaultdict
import requests
from bs4 import BeautifulSoup
import duckdb
import dns.resolver

SHARD=int(os.getenv('SHARD','0'))
SHARD_SIZE=int(os.getenv('SHARD_SIZE','10000'))
WORKERS=int(os.getenv('CRAWL_WORKERS','20'))
MAX_PAGES=int(os.getenv('MAX_PAGES','6'))
TIMEOUT=int(os.getenv('HTTP_TIMEOUT','12'))
RELEASE=os.getenv('OVERTURE_RELEASE','2026-08-19.0')
OUT='output'; os.makedirs(OUT,exist_ok=True)
PATH=f's3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*'
OFFSET=SHARD*SHARD_SIZE
MIN_LON,MAX_LON=51.3,56.7; MIN_LAT,MAX_LAT=22.5,26.6
UA='Mozilla/5.0 (compatible; UAE-B2B-Research/1.0; +public-business-research)'

ROLE_PATTERNS=[
 ('Chief Marketing Officer',r'chief\s+marketing\s+officer|\bCMO\b'),
 ('Chief Brand Officer',r'chief\s+brand\s+officer'),
 ('Chief Commercial Officer',r'chief\s+commercial\s+officer|\bCCO\b'),
 ('Chief Growth Officer',r'chief\s+growth\s+officer'),
 ('Chief Communications Officer',r'chief\s+communications?\s+officer'),
 ('Chief Executive Officer',r'chief\s+executive\s+officer|\bCEO\b'),
 ('Managing Director',r'managing\s+director'),
 ('General Manager',r'general\s+manager'),
 ('VP Marketing',r'(?:vice\s+president|vp)\s+(?:of\s+)?marketing'),
 ('Marketing Director',r'(?:marketing\s+director|director\s+of\s+marketing)'),
 ('Head of Marketing',r'head\s+of\s+marketing'),
 ('Marketing Manager',r'marketing\s+manager'),
 ('Brand Director',r'(?:brand\s+director|director\s+of\s+brand)'),
 ('Head of Brand',r'head\s+of\s+brand'),
 ('Brand Manager',r'brand\s+manager'),
 ('Partnerships Director',r'(?:partnerships?\s+director|director\s+of\s+partnerships?)'),
 ('Head of Partnerships',r'head\s+of\s+partnerships?'),
 ('Sponsorship Director',r'(?:sponsorship\s+director|director\s+of\s+sponsorship)'),
 ('Head of Sponsorship',r'head\s+of\s+sponsorship'),
 ('Commercial Director',r'(?:commercial\s+director|director\s+of\s+commercial)'),
 ('Business Development Director',r'(?:business\s+development\s+director|director\s+of\s+business\s+development)'),
 ('Head of Business Development',r'head\s+of\s+business\s+development'),
 ('Communications Director',r'(?:communications?\s+director|director\s+of\s+communications?)'),
 ('Head of Communications',r'head\s+of\s+communications?'),
 ('Media Director',r'(?:media\s+director|director\s+of\s+media)'),
 ('PR Director',r'(?:public\s+relations\s+director|pr\s+director|director\s+of\s+public\s+relations)'),
]
ROLE_RE='(?:'+'|'.join('(?:'+p+')' for _,p in ROLE_PATTERNS)+')'
# Human-name heuristic: 2-6 capitalized tokens, allowing common particles and initials.
NAME_TOKEN=r"(?:[A-Z][A-Za-z'’.-]{1,30}|Al|El|Bin|Ibn|Bint|Abd|Abdul|Mohamed|Mohammed|Muhammad|Md|Dr|Eng|Prof)"
NAME_RE=rf'(?:{NAME_TOKEN}\s+){{1,5}}{NAME_TOKEN}'
EMAIL_RE=re.compile(r'\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b',re.I)
BAD_EMAIL_PREFIX=('info@','contact@','hello@','sales@','support@','admin@','careers@','hr@','enquiry@','enquiries@','marketing@','media@','press@','pr@')
LINK_HINTS=('leadership','management','team','people','about','who-we-are','our-team','executive','contact','media','newsroom','corporate','company')
SOCIAL_HOSTS=('facebook.com','instagram.com','linkedin.com','twitter.com','x.com','youtube.com','tiktok.com','linktr.ee')
robot_cache={}; robot_lock=threading.Lock(); mx_cache={}; mx_lock=threading.Lock()


def clean_url(u):
 if not u:return ''
 u=str(u).strip()
 if not u.startswith(('http://','https://')):u='https://'+u
 return u

def host(u):
 try:return urlparse(u).netloc.lower().removeprefix('www.').split(':')[0]
 except:return ''
def same_domain(a,b):
 ha,hb=host(a),host(b)
 return ha==hb or ha.endswith('.'+hb) or hb.endswith('.'+ha)
def allowed(url):
 h=host(url)
 if not h or any(s in h for s in SOCIAL_HOSTS):return False
 with robot_lock:
  rp=robot_cache.get(h)
 if rp is None:
  rp=robotparser.RobotFileParser()
  rp.set_url(f'https://{h}/robots.txt')
  try: rp.read()
  except: pass
  with robot_lock:robot_cache[h]=rp
 try:return rp.can_fetch(UA,url)
 except:return True

def get_page(session,url):
 if not allowed(url):return None
 try:
  r=session.get(url,headers={'User-Agent':UA},timeout=TIMEOUT,allow_redirects=True)
  if r.status_code>=400:return None
  ct=r.headers.get('content-type','').lower()
  if 'text/html' not in ct and 'application/xhtml' not in ct:return None
  return r
 except:return None

def visible_text(soup):
 for t in soup(['script','style','noscript','svg']):t.decompose()
 return re.sub(r'\s+',' ',soup.get_text(' ',strip=True))
def canonical_role(title):
 for canon,p in ROLE_PATTERNS:
  if re.search(p,title,re.I):return canon
 return title.strip()
def role_family(role):
 s=role.lower()
 if 'marketing' in s:return 'Marketing'
 if 'brand' in s:return 'Brand'
 if 'partnership' in s or 'sponsor' in s:return 'Partnerships/Sponsorship'
 if 'commercial' in s or 'business development' in s or 'growth' in s:return 'Commercial/Growth'
 if 'communication' in s or 'media' in s or 'pr ' in (' '+s+' '):return 'Communications/Media/PR'
 if 'executive' in s or 'managing director' in s or 'general manager' in s:return 'Executive'
 return 'Other'
def plausible_name(n):
 n=re.sub(r'\s+',' ',n).strip(' -–—,:;|')
 if not (3<=len(n)<=90):return False
 low=n.lower()
 bad=('marketing','director','manager','officer','chief','company','group','leadership','team','business','commercial','communications','media','brand','partnership','sponsorship','general','executive','about','contact')
 if any(re.search(r'\b'+re.escape(x)+r'\b',low) for x in bad):return False
 toks=n.split()
 return 2<=len(toks)<=6 and sum(1 for x in toks if re.search(r'[A-Za-z]',x))>=2

def extract_people(text,url):
 found=[]; seen=set()
 # Name -> role and role -> name, within tight textual windows.
 pats=[
  re.compile(rf'(?P<name>{NAME_RE})\s*[,|\-–—:]{{1,3}}\s*(?P<title>{ROLE_RE})',re.I),
  re.compile(rf'(?P<title>{ROLE_RE})\s*[,|\-–—:]{{0,3}}\s*(?P<name>{NAME_RE})',re.I),
 ]
 for pat in pats:
  for m in pat.finditer(text):
   n=re.sub(r'^(Dr|Eng|Prof)\.?\s+','',m.group('name').strip())
   if not plausible_name(n):continue
   title=m.group('title').strip(); canon=canonical_role(title)
   k=(n.lower(),canon.lower())
   if k in seen:continue
   seen.add(k); found.append({'person_name':n,'title_observed':title,'canonical_role':canon,'role_family':role_family(canon),'person_source_url':url})
 return found

def public_emails(soup,text):
 vals=set(EMAIL_RE.findall(text))
 for a in soup.select('a[href^="mailto:"]'):
  v=a.get('href','')[7:].split('?')[0]
  vals.update(EMAIL_RE.findall(v))
 return sorted(vals)
def normalized_name_parts(name):
 s=re.sub(r"[^A-Za-z'’\- ]+",' ',name).lower().replace('’',"'")
 toks=[t.strip("'-") for t in s.split() if t.strip("'-")]
 toks=[t for t in toks if t not in ('dr','mr','mrs','ms','eng','prof')]
 if len(toks)<2:return ('','',[])
 first=toks[0]; last=toks[-1]
 variants=[last]
 if len(toks)>=3 and toks[-2] in ('al','el','bin','ibn','bint'):
  variants.insert(0,toks[-2]+last)
 if last.startswith('al') and len(last)>3:variants.append(last[2:])
 return first,last,list(dict.fromkeys(variants))
def permutations(name,domain):
 first,last,vars=normalized_name_parts(name)
 if not first or not last or not domain:return []
 out=[]
 for l in vars:
  out += [f'{first}.{l}@{domain}',f'{first}{l}@{domain}',f'{first[0]}.{l}@{domain}',f'{first[0]}{l}@{domain}',f'{first}@{domain}',f'{l}.{first}@{domain}',f'{first}.{l[0]}@{domain}',f'{first}{l[0]}@{domain}']
 return list(dict.fromkeys(out))
def match_person_email(name,emails,domain):
 f,l,vars=normalized_name_parts(name)
 if not f:return ''
 for e in emails:
  el=e.lower()
  if domain and not el.endswith('@'+domain):continue
  local=el.split('@')[0]
  clean=re.sub(r'[^a-z]','',local)
  if f in clean and any(v in clean for v in vars):return e
  if clean.startswith(f[0]) and any(v in clean for v in vars):return e
 return ''
def detect_pattern(name,email):
 f,l,vars=normalized_name_parts(name)
 if not f or '@' not in email:return ''
 local=email.lower().split('@')[0]
 for v in vars:
  patterns=[('{first}.{last}',f+'.'+v),('{first}{last}',f+v),('{f}.{last}',f[0]+'.'+v),('{f}{last}',f[0]+v),('{first}',f),('{last}.{first}',v+'.'+f),('{first}.{l}',f+'.'+v[0]),('{first}{l}',f+v[0])]
  for label,val in patterns:
   if local==val:return label
 return ''
def apply_pattern(name,domain,pattern):
 f,l,vars=normalized_name_parts(name)
 if not f or not domain:return ''
 v=vars[0] if vars else l
 local=pattern.replace('{first}',f).replace('{last}',v).replace('{f}',f[0]).replace('{l}',v[0])
 return local+'@'+domain

def has_mx(domain):
 if not domain:return False
 with mx_lock:
  if domain in mx_cache:return mx_cache[domain]
 try:
  ans=dns.resolver.resolve(domain,'MX',lifetime=4); ok=bool(ans)
 except:ok=False
 with mx_lock:mx_cache[domain]=ok
 return ok

def crawl_company(row):
 website=clean_url(row.get('website') or '')
 domain=host(website)
 if not website or not domain:return [],{'company_name':row['company_name'],'website':website,'status':'No website'}
 sess=requests.Session()
 first=get_page(sess,website)
 if not first:return [],{'company_name':row['company_name'],'website':website,'status':'Website unavailable/robots/disallowed'}
 pages=[]; queue=[first.url]; seen=set(); all_emails=set(); all_people=[]
 while queue and len(pages)<MAX_PAGES:
  u=queue.pop(0)
  if u in seen:continue
  seen.add(u)
  r=first if u==first.url else get_page(sess,u)
  if not r:continue
  try:soup=BeautifulSoup(r.text,'lxml')
  except:continue
  text=visible_text(soup); pages.append(r.url)
  em=public_emails(soup,text); all_emails.update(em)
  all_people.extend(extract_people(text,r.url))
  links=[]
  for a in soup.find_all('a',href=True):
   href=urljoin(r.url,a['href'].split('#')[0]); low=(href+' '+a.get_text(' ',strip=True)).lower()
   if same_domain(href,website) and any(x in low for x in LINK_HINTS):links.append(href)
  for href in links:
   if href not in seen and href not in queue:queue.append(href)
 # dedupe people; prefer first evidence page
 d={}
 for p in all_people:d.setdefault((p['person_name'].lower(),p['canonical_role'].lower()),p)
 people=list(d.values())
 # infer a domain pattern from person-linked public emails when available
 pattern_votes=defaultdict(int)
 for p in people:
  pe=match_person_email(p['person_name'],all_emails,domain)
  if pe:
   pat=detect_pattern(p['person_name'],pe)
   if pat:pattern_votes[pat]+=1
 best_pattern=max(pattern_votes,key=pattern_votes.get) if pattern_votes else ''
 best_votes=pattern_votes.get(best_pattern,0) if best_pattern else 0
 mx=has_mx(domain)
 out=[]
 for p in people:
  pe=match_person_email(p['person_name'],all_emails,domain)
  candidates=permutations(p['person_name'],domain)
  inferred=apply_pattern(p['person_name'],domain,best_pattern) if best_pattern and not pe else ''
  if inferred and inferred in candidates:candidates.remove(inferred); candidates.insert(0,inferred)
  source_type='Published person email' if pe else ('Pattern-inferred candidate' if inferred else 'Permutation candidates')
  confidence='Published' if pe else ('High pattern confidence' if best_votes>=2 else ('Low pattern confidence' if best_votes==1 else 'Unverified candidates'))
  out.append({
   'company_name':row['company_name'],'category':row.get('category',''),'website':website,'domain':domain,
   'person_name':p['person_name'],'current_title':p['title_observed'],'canonical_role':p['canonical_role'],'role_family':p['role_family'],
   'currentness_basis':'Listed on company website at research time','person_source_url':p['person_source_url'],'research_date':'2026-09-02',
   'published_person_email':pe,'published_company_emails':'; '.join(sorted(all_emails)),
   'observed_email_pattern':best_pattern,'pattern_evidence_count':best_votes,'preferred_inferred_email':inferred,
   'email_permutation_candidates':'; '.join(candidates[:16]),'email_source_type':source_type,'email_confidence':confidence,
   'domain_has_mx':'Yes' if mx else 'No/Unknown','mailbox_verification_status':'Not mailbox-verified',
   'public_business_phones':row.get('public_business_phones',''),'social_urls':row.get('social_urls',''),
   'pages_crawled':'; '.join(pages),'base_source':f'Overture Maps Places {RELEASE}'
  })
 return out,{'company_name':row['company_name'],'website':website,'status':'Crawled','people_found':len(out),'pages':len(pages),'public_emails':len(all_emails)}

# Query deterministic 10K slice of UAE Overture places.
con=duckdb.connect(); con.execute('INSTALL httpfs'); con.execute('LOAD httpfs'); con.execute("SET s3_region='us-west-2'")
q=f"""
WITH uae AS (
 SELECT id AS overture_id,names.primary AS company_name,
 COALESCE(taxonomy.primary,categories.primary,basic_category,'') AS category,
 list_extract(websites,1) AS website,array_to_string(emails,'; ') AS published_company_emails,
 array_to_string(phones,'; ') AS public_business_phones,array_to_string(socials,'; ') AS social_urls,
 list_extract(addresses,1).locality AS locality,list_extract(addresses,1).country AS country_code,
 confidence,operating_status,bbox.xmin AS longitude,bbox.ymin AS latitude
 FROM read_parquet('{PATH}')
 WHERE bbox.xmin BETWEEN {MIN_LON} AND {MAX_LON} AND bbox.ymin BETWEEN {MIN_LAT} AND {MAX_LAT}
 AND COALESCE(operating_status,'open') <> 'permanently_closed' AND names.primary IS NOT NULL AND length(trim(names.primary))>1
), ranked AS (
 SELECT *,lower(regexp_replace(trim(company_name),'[^a-zA-Z0-9]+',' ','g')) name_key,
 row_number() over(partition by lower(regexp_replace(trim(company_name),'[^a-zA-Z0-9]+',' ','g')),coalesce(lower(website),''),coalesce(lower(locality),'') order by confidence desc nulls last) rn
 FROM uae WHERE country_code IS NULL OR country_code='AE'
)
SELECT * EXCLUDE(name_key,rn) FROM ranked WHERE rn=1 ORDER BY confidence DESC NULLS LAST,company_name LIMIT {SHARD_SIZE} OFFSET {OFFSET}
"""
rows=con.execute(q).df().fillna('').to_dict('records')
print(f'Shard {SHARD}: {len(rows)} companies, offset {OFFSET}',flush=True)

people=[]; audit=[]
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
 futs={ex.submit(crawl_company,r):r for r in rows}
 for i,f in enumerate(as_completed(futs),1):
  try:p,a=f.result(); people.extend(p); audit.append(a)
  except Exception as e:audit.append({'company_name':futs[f].get('company_name',''),'website':futs[f].get('website',''),'status':'Error: '+str(e)[:180]})
  if i%250==0:print(f'shard {SHARD}: {i}/{len(rows)} companies, people={len(people)}',flush=True)

people_file=f'{OUT}/UAE_people_shard_{SHARD:02d}.csv'; audit_file=f'{OUT}/UAE_audit_shard_{SHARD:02d}.csv'
people_fields=['company_name','category','website','domain','person_name','current_title','canonical_role','role_family','currentness_basis','person_source_url','research_date','published_person_email','published_company_emails','observed_email_pattern','pattern_evidence_count','preferred_inferred_email','email_permutation_candidates','email_source_type','email_confidence','domain_has_mx','mailbox_verification_status','public_business_phones','social_urls','pages_crawled','base_source']
with open(people_file,'w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=people_fields);w.writeheader();w.writerows(people)
audit_fields=['company_name','website','status','people_found','pages','public_emails']
with open(audit_file,'w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=audit_fields,extrasaction='ignore');w.writeheader();w.writerows(audit)
summary={'shard':SHARD,'companies':len(rows),'crawled':sum(a.get('status')=='Crawled' for a in audit),'people_found':len(people),'published_person_emails':sum(bool(p['published_person_email']) for p in people),'inferred_preferred_emails':sum(bool(p['preferred_inferred_email']) for p in people)}
with open(f'{OUT}/summary_{SHARD:02d}.json','w') as f:json.dump(summary,f,indent=2)
print(json.dumps(summary,indent=2),flush=True)
