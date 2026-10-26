"""Collect one day of policy news; preserve raw XML and article license values."""
import argparse
import datetime as dt
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = 'https://apis.data.go.kr/1371000/policyNewsService2/policyNewsList2'

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.hidden += 1
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'li'): self.parts.append('\n')
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=(dt.date.today() - dt.timedelta(days=1)).strftime('%Y%m%d'))
    args = parser.parse_args()
    dt.datetime.strptime(args.date, '%Y%m%d')
    key = os.environ.get('DATA_GO_KR_SERVICE_KEY', '')
    env = ROOT / '.env'
    if not key and env.exists():
        for line in env.read_text().splitlines():
            name, sep, value = line.partition('=')
            if sep and name.strip() == 'DATA_GO_KR_SERVICE_KEY':
                key = value.strip().strip('\"\'')
    if not key:
        raise SystemExit('DATA_GO_KR_SERVICE_KEY is missing. Set it in the project .env.')
    query = urllib.parse.urlencode(dict(serviceKey=urllib.parse.unquote(key), startDate=args.date, endDate=args.date))
    try:
        with urllib.request.urlopen(ENDPOINT + '?' + query, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise SystemExit(f'API HTTP error: {error.code}; request URL withheld.') from None
    except urllib.error.URLError:
        raise SystemExit('API connection failed; request URL withheld.') from None
    out = Path(__file__).resolve().parent / args.date
    out.mkdir(exist_ok=True)
    (out / 'response.xml').write_bytes(raw)
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError:
        raise SystemExit('Response is not XML; inspect response.xml.') from None
    articles = []
    for item in tree.iter():
        if item.tag.split('}')[-1] != 'NewsItem': continue
        record = {child.tag.split('}')[-1]: ''.join(child.itertext()) for child in item}
        body = record.get('DataContents', '')
        if record.get('ContentsType') == 'H':
            cleaner = TextParser()
            cleaner.feed(body)
            body = ''.join(cleaner.parts)
        body = '\n\n'.join(' '.join(line.split()) for line in body.splitlines() if line.strip())
        record.update(text=body, char_count=len(body), license_review='pending', collected_at=dt.datetime.now(dt.timezone.utc).isoformat())
        articles.append(record)
    (out / 'articles.json').write_text(json.dumps(articles, ensure_ascii=False, indent=2))
    preview = ['# Policy news extraction', '', 'License review pending; no publication approval inferred.', '']
    for record in articles:
        preview += [f"## {record.get('Title', '')}", f"Characters: {record['char_count']} | KoglType: {record.get('KoglType', '')}", record.get('OriginalUrl', ''), '', record['text'], '']
    (out / 'preview.md').write_text('\n'.join(preview))
    print(f'Extracted {len(articles)} articles. Saved to {out}')
    if not articles:
        print('Empty result: inspect response.xml for API error or no-data status.')

if __name__ == '__main__':
    main()
