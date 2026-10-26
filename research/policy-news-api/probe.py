import json, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter
from collect import ROOT, ENDPOINT, TextParser

key = next(line.partition('=')[2].strip().strip('\"\'') for line in (ROOT/'.env').read_text().splitlines() if line.partition('=')[0].strip()=='DATA_GO_KR_SERVICE_KEY')
out=Path(__file__).parent/'probe-20261012'
out.mkdir(exist_ok=True)
queries=[(f'202610{d:02}',f'202610{d:02}') for d in range(5,12)]+[('20261005','20261007'),('20261005','20261009'),('20251001','20251001'),('20221004','20221004')]
results=[]
for start,end in queries:
    q=urllib.parse.urlencode(dict(serviceKey=urllib.parse.unquote(key),startDate=start,endDate=end))
    try:
        with urllib.request.urlopen(ENDPOINT+'?'+q,timeout=40) as response: raw=response.read()
        (out/f'{start}-{end}.xml').write_bytes(raw)
        root=ET.fromstring(raw)
        for e in root.iter(): e.tag=e.tag.split('}')[-1]
        records=[{e.tag:''.join(e.itertext()) for e in item} for item in root.iter('NewsItem')]
        lengths=[]
        for r in records:
            p=TextParser();p.feed(r.get('DataContents',''))
            lengths.append(len('\n\n'.join(' '.join(s.split()) for s in ''.join(p.parts).splitlines() if s.strip())))
        result=dict(start=start,end=end,code=root.findtext('.//resultCode'),message=root.findtext('.//resultMsg'),total=root.findtext('.//totalCount'),records=len(records),unique_ids=len(set(r.get('NewsItemId') for r in records)),long_1500=sum(n>=1500 for n in lengths),licenses=dict(Counter(r.get('KoglType') for r in records)),statuses=dict(Counter(r.get('ContentsStatus') for r in records)),dates=[{k:r.get(k) for k in ['NewsItemId','ModifyId','ModifyDate','ApproveDate','EmbargoDate']} for r in records])
    except Exception as e:
        result=dict(start=start,end=end,error=type(e).__name__)
    results.append(result)
    print(json.dumps({k:v for k,v in result.items() if k!='dates'},ensure_ascii=False),flush=True)
(out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
