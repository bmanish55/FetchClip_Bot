import re, sys
import httpx

url = sys.argv[1]
r = httpx.get(
    url,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"},
    follow_redirects=True,
    timeout=20,
)
t = r.text
print("STATUS:", r.status_code, "| LENGTH:", len(t))
print("CLOUDFLARE:", any(k in t.lower() for k in ("cf-chl", "just a moment", "cloudflare")))
print("\nIFRAMES:")
for m in re.findall(r"<iframe[^>]+>", t, re.I)[:10]:
    print(" ", m[:200])
print("\nMEDIA LINKS:")
for m in re.findall(r"https?:[^\s\"'<>]+?\.(?:m3u8|mp4)[^\s\"'<>]*", t.replace("\\/", "/"))[:10]:
    print(" ", m[:200])
print("\nDATA LINKS:")
for m in re.findall(r"(?:data-(?:src|url|link|video)|href)=[\"']([^\"']*(?:dood|stream|mixdrop|filemoon|vidhide|upstream|embed|player|drive|gdrive)[^\"']*)", t, re.I)[:15]:
    print(" ", m[:200])