
#!/usr/bin/env python3
# Minimal starter version. Requires:
# pip install google-api-python-client google-auth google-auth-oauthlib requests

import re,csv,requests
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES=["https://www.googleapis.com/auth/spreadsheets.readonly"]
BASE=Path(__file__).parent
DATA_DIR=BASE/"data"; DATA_DIR.mkdir(exist_ok=True)
COMPANY_FILE=BASE/"companies.txt"

def sanitize(s):
    return re.sub(r'[\\/:*?"<>|]','_',s.strip().replace(" ","_"))

def get_creds():
    token=BASE/"token.json"
    creds=None
    if token.exists():
        creds=Credentials.from_authorized_user_file(token,SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow=InstalledAppFlow.from_client_secrets_file(str(BASE/"credentials.json"),SCOPES)
            creds=flow.run_local_server(port=0)
        token.write_text(creds.to_json())
    return creds

def export_url(url):
    m=re.search(r"/d/([\w-]+)",url)
    g=re.search(r"gid=(\d+)",url)
    if not m:return None
    gid=g.group(1) if g else "0"
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=tsv&gid={gid}"

def main():
    creds=get_creds(); creds.refresh(Request())
    s=requests.Session(); s.headers["Authorization"]="Bearer "+creds.token
    with COMPANY_FILE.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f,delimiter="\t"):
            name=(row.get("Company Name") or "").strip()
            if not name: continue
            safe=sanitize(name)
            for col,suffix in [("Student Response Sheet","responses"),("Company Sheet","shortlists")]:
                url=(row.get(col) or "").strip()
                if not url or "docs.google.com" not in url: continue
                e=export_url(url)
                if not e: continue
                r=s.get(e,timeout=60)
                if r.status_code==200:
                    (DATA_DIR/f"{safe}_{suffix}.txt").write_text(r.text,encoding="utf-8")
                    print("Downloaded",name,suffix)
                else:
                    print("Failed",name,suffix,r.status_code)

if __name__=="__main__":
    main()
