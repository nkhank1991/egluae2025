import os, re, json
from urllib.parse import quote_plus, urlparse
import duckdb
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

LIMIT = int(os.getenv('UAE_LIMIT', '100000'))
OUT = 'output'
os.makedirs(OUT, exist_ok=True)
RELEASE = os.getenv('OVERTURE_RELEASE', '2026-08-19.0')
PATH = f's3://overturemaps-us-west-2/release/{RELEASE}/theme=places/type=place/*'

# UAE geographic envelope. Country code is also checked where present.
MIN_LON, MAX_LON = 51.3, 56.7
MIN_LAT, MAX_LAT = 22.5, 26.6


def clean_domain(url):
    if not url or str(url).lower() in ('nan', 'none'):
        return ''
    s = str(url).strip()
    if not s.startswith(('http://', 'https://')):
        s = 'https://' + s
    try:
        return urlparse(s).netloc.lower().replace('www.', '').split(':')[0]
    except Exception:
        return ''


def save_excel(df, path):
    wb = Workbook()
    ws = wb.active
    ws.title = 'UAE 100K Companies'
    ws.append(list(df.columns))
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='1F4E78')
        c.alignment = Alignment(wrap_text=True, vertical='top')
    for row in df.fillna('').itertuples(index=False, name=None):
        ws.append(list(row))
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    widths = {
        'A': 38, 'B': 26, 'C': 24, 'D': 36, 'E': 28, 'F': 22, 'G': 24,
        'H': 42, 'I': 18, 'J': 22, 'K': 35, 'L': 35, 'M': 20, 'N': 18
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows():
        for c in row:
            c.alignment = Alignment(vertical='top', wrap_text=True)
    wb.save(path)


con = duckdb.connect()
con.execute("INSTALL httpfs")
con.execute("LOAD httpfs")
con.execute("SET s3_region='us-west-2'")

# Overture Places is a real-world places/business dataset. We pull UAE records by
# both bounding box and address country when available, remove permanently closed
# places, and keep higher-confidence records first.
query = f"""
WITH uae AS (
  SELECT
    id AS overture_id,
    names.primary AS company_name,
    COALESCE(taxonomy.primary, categories.primary, basic_category, '') AS category,
    COALESCE(basic_category, '') AS basic_category,
    confidence,
    operating_status,
    list_extract(websites, 1) AS website,
    array_to_string(websites, '; ') AS websites_all,
    array_to_string(emails, '; ') AS published_company_emails,
    array_to_string(phones, '; ') AS public_business_phones,
    array_to_string(socials, '; ') AS social_urls,
    list_extract(addresses, 1).freeform AS address,
    list_extract(addresses, 1).locality AS locality,
    list_extract(addresses, 1).region AS region,
    list_extract(addresses, 1).postcode AS postcode,
    list_extract(addresses, 1).country AS country_code,
    bbox.xmin AS longitude,
    bbox.ymin AS latitude
  FROM read_parquet('{PATH}')
  WHERE bbox.xmin BETWEEN {MIN_LON} AND {MAX_LON}
    AND bbox.ymin BETWEEN {MIN_LAT} AND {MAX_LAT}
    AND COALESCE(operating_status, 'open') <> 'permanently_closed'
    AND names.primary IS NOT NULL
    AND length(trim(names.primary)) > 1
), ranked AS (
  SELECT *,
    lower(regexp_replace(trim(company_name), '[^a-zA-Z0-9]+', ' ', 'g')) AS name_key,
    row_number() OVER (
      PARTITION BY lower(regexp_replace(trim(company_name), '[^a-zA-Z0-9]+', ' ', 'g')),
                   COALESCE(lower(website), ''),
                   COALESCE(lower(locality), '')
      ORDER BY confidence DESC NULLS LAST
    ) AS rn
  FROM uae
  WHERE country_code IS NULL OR country_code = 'AE'
)
SELECT * EXCLUDE (name_key, rn)
FROM ranked
WHERE rn = 1
ORDER BY confidence DESC NULLS LAST, company_name
LIMIT {LIMIT}
"""

print(f'Querying Overture {RELEASE} for up to {LIMIT:,} UAE business/place records...', flush=True)
df = con.execute(query).df()
print(f'Overture UAE rows returned: {len(df):,}', flush=True)

# Add B2B research fields. These are deliberately explicit about what is and is
# not yet researched/verified; no fabricated executives or emails.
df['domain'] = df['website'].map(clean_domain)
df['country'] = 'United Arab Emirates'
df['source'] = f'Overture Maps Places {RELEASE}'
df['source_url'] = 'https://docs.overturemaps.org/guides/places/'
df['research_date'] = '2026-09-02'
df['meta_ad_library_url'] = df['company_name'].map(
    lambda n: 'https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=AE&q=' + quote_plus(str(n))
)
for c in [
    'ceo_md_gm', 'ceo_title', 'primary_marketing_brand_leader', 'marketing_title',
    'additional_commercial_partnerships_contacts', 'person_linkedin',
    'observed_email_pattern', 'email_permutation_candidates', 'mailbox_verification_status',
    'business_mobile_enrichment', 'meta_campaign_intelligence', 'inferred_audience_cohorts',
    'public_martech_signals'
]:
    df[c] = ''

df['enrichment_status'] = 'Base company/place record extracted; executive/marketing enrichment pending'
df['email_note'] = df['published_company_emails'].map(
    lambda x: 'Published business email from source; mailbox not independently verified' if str(x).strip() not in ('', 'nan', 'None') else 'No published email in base source'
)

# Commercially useful ordering for the master workbook.
front = [
    'company_name', 'category', 'basic_category', 'website', 'domain', 'published_company_emails',
    'public_business_phones', 'social_urls', 'address', 'locality', 'region', 'postcode',
    'country', 'confidence', 'operating_status', 'ceo_md_gm', 'ceo_title',
    'primary_marketing_brand_leader', 'marketing_title',
    'additional_commercial_partnerships_contacts', 'person_linkedin',
    'observed_email_pattern', 'email_permutation_candidates', 'mailbox_verification_status',
    'business_mobile_enrichment', 'meta_ad_library_url', 'meta_campaign_intelligence',
    'inferred_audience_cohorts', 'public_martech_signals', 'enrichment_status', 'email_note',
    'source', 'source_url', 'research_date', 'overture_id', 'longitude', 'latitude', 'websites_all'
]
df = df[[c for c in front if c in df.columns]]

csv_path = f'{OUT}/UAE_100K_Companies.csv'
xlsx_path = f'{OUT}/UAE_100K_Master.xlsx'
parquet_path = f'{OUT}/UAE_100K_Companies.parquet'
df.to_csv(csv_path, index=False)
df.to_parquet(parquet_path, index=False)
save_excel(df, xlsx_path)

summary = {
    'requested_limit': LIMIT,
    'companies_extracted': int(len(df)),
    'with_website': int(df['website'].fillna('').astype(str).str.strip().ne('').sum()),
    'with_published_company_email': int(df['published_company_emails'].fillna('').astype(str).str.strip().ne('').sum()),
    'with_public_business_phone': int(df['public_business_phones'].fillna('').astype(str).str.strip().ne('').sum()),
    'with_social_url': int(df['social_urls'].fillna('').astype(str).str.strip().ne('').sum()),
    'source': f'Overture Maps Places {RELEASE}',
    'master_excel': 'UAE_100K_Master.xlsx',
    'note': 'This is the real UAE company/place base layer. Executive/marketing names, inferred emails, mailbox verification, business-mobile enrichment and campaign intelligence remain separate enrichment stages and are never fabricated.'
}
with open(f'{OUT}/summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)
print(json.dumps(summary, indent=2), flush=True)
