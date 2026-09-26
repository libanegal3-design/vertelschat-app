"""Screenshot runner for the family web app. Usage: python shoot.py <name> <path> <widths,...> [--user email] [--full]"""
import argparse, os, sys, json
from playwright.sync_api import sync_playwright

BASE = os.environ.get("VT_BASE", "http://127.0.0.1:8000")
OUT = os.environ.get("VT_SHOTS", "/root/work/vt/screens/app")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs", help="JSON list of [name, path, widths, user|null, full(bool), extra(dict)]")
    args = ap.parse_args()
    jobs = json.loads(open(args.jobs).read()) if os.path.exists(args.jobs) else json.loads(args.jobs)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        contexts = {}
        for name, path, widths, user, full, *extra in jobs:
            extra = extra[0] if extra else {}
            for w in widths:
                h = 900 if w >= 1024 else (1024 if w >= 768 else 780)
                key = (user, w)
                if key not in contexts:
                    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1 if w >= 1024 else 2,
                                              locale="nl-NL", timezone_id="Europe/Amsterdam")
                    if user:
                        pg = ctx.new_page(); pg.goto(f"{BASE}/dev/login?email={user}"); pg.close()
                    contexts[key] = ctx
                page = contexts[key].new_page()
                page.goto(BASE + path, wait_until="networkidle")
                page.wait_for_timeout(extra.get("wait", 250))
                for sel in extra.get("click", []):
                    page.click(sel); page.wait_for_timeout(200)
                if extra.get("scroll"):
                    page.evaluate(f"window.scrollTo(0, {extra['scroll']})")
                for sel, val in extra.get("fill", {}).items():
                    page.fill(sel, val); page.wait_for_timeout(100)
                fn = f"{OUT}/{name}-{w}.png"
                if extra.get("element"):
                    page.locator(extra["element"]).first.screenshot(path=fn)
                else:
                    page.screenshot(path=fn, full_page=full)
                print(fn)
                page.close()
        browser.close()

if __name__ == "__main__":
    main()
