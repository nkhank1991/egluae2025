import os,re,json,unicodedata
from urllib.parse import quote_plus,urlparse
import pandas as pd
import duckdb
from huggingface_hub import HfApi
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill,Alignment

LIMIT=int(os.getenv('UAE_LIMIT','10000'))
OUT='output'; os.makedirs(OUT,exist_ok=True)
REPO='hugging-science/opendata'
TARGET_RE=r'(chief executive|\bceo\b|managing director|general manager|chief marketing|\bcmo\b|marketing director|head of marketing|marketing manager|brand director|head of brand|partnership|sponsorship|commercial director|chief commercial|communications director|head of communications|media director|public relations|\bpr director\b|business development director|head of business development)'

def q(s): return "'"+str(s).replace("'","''")+"'"
def norm(s):
    s=unicodedata.normalize('NFKD',str(s)).encode('ascii','ignore').decode().lower()
    s=re.sub(r'[^a-z0-9 ]+',' ',s); return re.sub(r'\s+',' ',s).strip()
def domain(w):
    if not w:return ''
    w=str(w).strip(); w=w if w.startswith('http') else 'https://'+w
    try:return urlparse(w).netloc.lower().replace('www.','').split(':')[0]
    except:return ''
def parts(name):
    ws=norm(name).split()
    if len(ws)<2:return '',''
    f=ws[0]; l=''.join(ws[-2:]) if len(ws)>=3 and ws[-2] in {'al','el','bin','ibn','bint'} else ws[-1]
    return f,l
def perms(name,d):
    f,l=parts(name)
    if not f or not l or not d:return ''
    return '; '.join(dict.fromkeys([f'{f}.{l}@{d}',f'{f[0]}{l}@{d}',f'{f[0]}.{l}@{d}',f'{f}{l}@{d}',f'{f}@{d}',f'{l}.{f}@{d}']))

def choose(cols,candidates,contains=()):
    lc={c.lower():c for c in cols}
    for x in candidates:
        if x.lower() in lc:return lc[x.lower()]
    for c in cols:
        cl=c.lower()
        if any(x in cl for x in contains):return c
    return None

def urls_for(prefix):
    api=HfApi(); files=api.list_repo_files(REPO,repo_type='dataset')
    paths=[p for p in files if p.startswith(prefix) and p.endswith('.parquet')]
    if not paths:
        raise RuntimeError(f'No parquet files under {prefix}. Found examples: {files[:20]}')
    return [f'https://huggingface.co/datasets/{REPO}/resolve/main/{p}' for p in paths]

con=duckdb.connect()
con.execute('INSTALL httpfs; LOAD httpfs;')
con.execute("SET threads=4")
con.execute("SET memory_limit='5GB'")

company_urls=urls_for('data/companies/parquet/')
company_src='['+','.join(q(u) for u in company_urls)+']'
company_cols=[r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({company_src}, union_by_name=true) LIMIT 0").fetchall()]
print('COMPANY COLS',company_cols)
name_c=choose(company_cols,['bq_company_name','company_name','name'],contains=('company_name','name'))
id_c=choose(company_cols,['bq_id','company_id','id'],contains=('company_id','bq_id'))
country2=choose(company_cols,['bq_company_address1_country_code2','country_code2','country_code'],contains=('country_code2',))
country3=choose(company_cols,['bq_company_address1_country_code3'],contains=('country_code3',))
website_c=choose(company_cols,['bq_website','website','domain'],contains=('website',))
legal_c=choose(company_cols,['bq_company_legal_name','legal_name'],contains=('legal_name',))
city_c=choose(company_cols,['bq_company_address1_city','city'],contains=('address1_city',))
state_c=choose(company_cols,['bq_company_address1_state','state','emirate'],contains=('address1_state',))
addr1=choose(company_cols,['bq_company_address1_line_1'],contains=('address1_line_1',))
addr2=choose(company_cols,['bq_company_address1_line_2'],contains=('address1_line_2',))
linkedin_c=choose(company_cols,['bq_company_linkedin_url','linkedin_url'],contains=('linkedin_url',))
lei_c=choose(company_cols,['bq_company_lei','lei'],contains=('company_lei',))
ticker_c=choose(company_cols,['bq_ticker','ticker'],contains=('ticker',))
if not (name_c and id_c and (country2 or country3)): raise RuntimeError('Required company columns not found')
where=[]
if country2: where.append(f"upper(coalesce(cast({q(country2)} as varchar),'')) in ('AE','ARE','UNITED ARAB EMIRATES')")
if country3: where.append(f"upper(coalesce(cast({q(country3)} as varchar),''))='ARE'")
# quote identifiers properly
I=lambda c: '"'+c.replace('"','""')+'"' if c else "''"
where=[]
if country2: where.append(f"upper(coalesce(cast({I(country2)} as varchar),'')) in ('AE','ARE','UNITED ARAB EMIRATES')")
if country3: where.append(f"upper(coalesce(cast({I(country3)} as varchar),''))='ARE'")
selects={
 'company_id':id_c,'company_name':name_c,'legal_name':legal_c,'website':website_c,'city':city_c,'state_emirate':state_c,
 'addr1':addr1,'addr2':addr2,'linkedin_company':linkedin_c,'lei':lei_c,'ticker':ticker_c}
sel=[]
for alias,c in selects.items(): sel.append(f"cast({I(c)} as varchar) as {alias}" if c else f"'' as {alias}")
sql=f"SELECT {','.join(sel)} FROM read_parquet({company_src}, union_by_name=true) WHERE ({' OR '.join(where)}) AND {I(name_c)} IS NOT NULL LIMIT {LIMIT}"
print('Querying UAE companies...')
companies=con.execute(sql).df()
companies['domain']=companies['website'].map(domain)
companies['address']=(companies['addr1'].fillna('')+' '+companies['addr2'].fillna('')).str.strip()
companies['country']='United Arab Emirates'; companies['source']='OpenData Consortium / Hugging Face'; companies['source_date']='2026-09-02'
companies.drop(columns=['addr1','addr2'],inplace=True)
print('Companies',len(companies))
con.register('target_companies',companies[['company_id']])

# Enrich from people parquet using a join to only the selected company IDs.
contacts=pd.DataFrame()
try:
    people_urls=urls_for('data/people/parquet/')
    people_src='['+','.join(q(u) for u in people_urls)+']'
    pcols=[r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({people_src}, union_by_name=true) LIMIT 0").fetchall()]
    print('PEOPLE COLS',pcols)
    pcid=choose(pcols,['bq_company_id','company_id','employer_company_id','current_company_id','bq_employer_id'],contains=('company_id','employer_id'))
    ptitle=choose(pcols,['bq_job_title','job_title','title','position','current_title'],contains=('job_title','current_title'))
    pname=choose(pcols,['bq_full_name','full_name','name','person_name'],contains=('full_name','person_name'))
    pfirst=choose(pcols,['bq_first_name','first_name'],contains=('first_name',)); plast=choose(pcols,['bq_last_name','last_name'],contains=('last_name',))
    pemail=choose(pcols,['bq_work_email','work_email','email','business_email'],contains=('work_email','business_email'))
    pphone=choose(pcols,['bq_phone','phone','mobile_phone','work_phone'],contains=('phone',))
    plink=choose(pcols,['bq_linkedin_url','linkedin_url','person_linkedin_url'],contains=('linkedin_url',))
    if pcid and ptitle:
        name_expr=f"cast({I(pname)} as varchar)" if pname else (f"concat_ws(' ',cast({I(pfirst)} as varchar),cast({I(plast)} as varchar))" if pfirst or plast else "''")
        fields=[f"cast(p.{I(pcid)} as varchar) company_id",f"{name_expr} person_name",f"cast(p.{I(ptitle)} as varchar) person_title",
                f"cast(p.{I(pemail)} as varchar) published_work_email" if pemail else "'' published_work_email",
                f"cast(p.{I(pphone)} as varchar) business_phone_or_mobile" if pphone else "'' business_phone_or_mobile",
                f"cast(p.{I(plink)} as varchar) person_linkedin" if plink else "'' person_linkedin"]
        psql=f"""SELECT * EXCLUDE(rn) FROM (
          SELECT {','.join(fields)}, row_number() over(partition by cast(p.{I(pcid)} as varchar) order by length(cast(p.{I(ptitle)} as varchar)) desc) rn
          FROM read_parquet({people_src}, union_by_name=true) p
          JOIN target_companies t ON cast(p.{I(pcid)} as varchar)=cast(t.company_id as varchar)
          WHERE regexp_matches(lower(cast(p.{I(ptitle)} as varchar)), {q(TARGET_RE)})
        ) WHERE rn<=8"""
        print('Querying target executives...')
        contacts=con.execute(psql).df()
        print('Contacts',len(contacts))
except Exception as e:
    print('People enrichment warning',repr(e))

if contacts.empty:
    master=companies.copy(); master['person_name']=''; master['person_title']=''; master['published_work_email']=''; master['business_phone_or_mobile']=''; master['person_linkedin']=''
else:
    master=companies.merge(contacts,on='company_id',how='left')
for c in ['person_name','person_title','published_work_email','business_phone_or_mobile','person_linkedin']:
    if c not in master: master[c]=''
master['email_permutations']=master.apply(lambda r: perms(r.get('person_name',''),r.get('domain','')),axis=1)
master['email_status']=master.apply(lambda r:'Published' if str(r.get('published_work_email','')).strip() not in ('','nan','None') else ('Candidate only' if str(r.get('person_name','')).strip() not in ('','nan','None') and r.get('domain') else 'Not found'),axis=1)
master['meta_ad_library_url']=master['company_name'].map(lambda n:'https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AE&q='+quote_plus(str(n)))
master['meta_cohort_note']='Private Meta audiences/cookies are not public; cohort field requires inference from public campaigns.'
master['martech_status']='Queued for public website scan'
master['validation_status']=master['email_status'].map(lambda x:'Exact public data' if x=='Published' else 'Not mailbox-verified')

master.to_csv(f'{OUT}/UAE_10K_Master.csv',index=False); companies.to_csv(f'{OUT}/UAE_10K_Companies.csv',index=False)
wb=Workbook(); ws=wb.active; ws.title='UAE 10K Master'; ws.append(list(master.columns))
for c in ws[1]: c.font=Font(bold=True,color='FFFFFF'); c.fill=PatternFill('solid',fgColor='1F4E78'); c.alignment=Alignment(wrap_text=True)
for row in master.itertuples(index=False,name=None): ws.append(list(row))
ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
for col in ws.columns:
    letter=col[0].column_letter; ws.column_dimensions[letter].width=min(max(12,max(len(str(x.value or '')) for x in col[:150])+2),42)
for row in ws.iter_rows():
    for c in row: c.alignment=Alignment(vertical='top',wrap_text=True)
wb.save(f'{OUT}/UAE_10K_Master.xlsx')
summary={'companies':int(len(companies)),'master_rows':int(len(master)),'contacts_found':int(len(contacts)),'company_columns':company_cols,'people_columns':pcols if 'pcols' in locals() else [],'file':'UAE_10K_Master.xlsx'}
with open(f'{OUT}/summary.json','w') as f: json.dump(summary,f,indent=2)
print(json.dumps({k:v for k,v in summary.items() if k not in ('company_columns','people_columns')},indent=2))
