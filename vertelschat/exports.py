"""'Download alles': a complete archive in open formats that works without Vertelschat.

ZIP layout (Dutch folder names for Dutch families; English equivalents in documentation):
  LEESMIJ.txt  index.html (offline viewer)  audio/  transcripties/  verhalen/  fotos/  overige-bestanden/
  boek/  projectgegevens/ (project.json, verhalen.csv, qr-codes.csv, CHECKSUMS-sha256.txt)"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import tempfile
import unicodedata
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics import track
from .audio import format_duration
from .db import utcnow
from .jobs import enqueue, job
from .models import (Book, BookVersion, Export, MediaAsset, Photo, Project, Prompt, QRLink, Recording, Story,
                     Transcript)
from .notify import notify_organizers
from .qr import qr_url
from .storage import ext_for_mime, get_storage, store_path

KEEP_EXPORTS = 2


def slug(text: str, maxlen: int = 48) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:maxlen].rstrip("-")) or "verhaal"


def request_export(session: Session, project: Project, requested_by_id: str | None) -> Export:
    running = session.scalar(select(Export).where(Export.project_id == project.id,
                                                  Export.status.in_(["queued", "building"])))
    if running is not None:
        return running
    export = Export(project_id=project.id, requested_by_id=requested_by_id, status="queued")
    session.add(export)
    session.flush()
    enqueue(session, "build_export", {"export_id": export.id}, dedupe_key=f"export:{export.id}")
    return export


def _readme(project: Project, when: str) -> str:
    name = project.storyteller.name if project.storyteller else "de verteller"
    return f"""HET ARCHIEF VAN {project.title.upper()}

Dit archief is van jullie. Je hebt Vertelschat niet nodig om het te openen of te gebruiken.

WAT ZIT ERIN?
- index.html        Open dit bestand in je browser (dubbelklikken). Je kunt alle verhalen lezen en
                    beluisteren, ook zonder internet.
- audio/            De originele spraakberichten van {name}, precies zoals ze via WhatsApp binnenkwamen
                    (meestal .ogg), plus een kopie in .mp3 die op elk apparaat afspeelt.
                    Elke bestandsnaam begint met de audiocode uit het boek, bijvoorbeeld A07.
- transcripties/    De letterlijke uitgeschreven tekst van elke opname.
- verhalen/         De bewerkte verhalen zoals ze in het boek staan (gewone tekst, Markdown).
- fotos/            Alle foto's in het oorspronkelijke formaat.
- overige-bestanden/ Video's en documenten die via WhatsApp zijn gestuurd.
- boek/             Het boek als pdf (binnenwerk voor de drukker, omslag en digitale editie).
- projectgegevens/  Alle gegevens als JSON en CSV, en CHECKSUMS-sha256.txt: controlegetallen waarmee je kunt
                    nagaan of een bestand onbeschadigd is.

TIP
Bewaar een kopie op minstens twee plekken, bijvoorbeeld op je computer en op een usb-stick of in je eigen
cloudopslag. Je kunt dit archief later altijd opnieuw downloaden in Vertelschat, ook na afloop van het verteljaar.

De QR-codes in het boek blijven werken. Werkt een code ooit niet, zoek dan de audiocode op in de map audio/.

Gemaakt op {when}.
"""


def _viewer(project: Project, entries: list[dict]) -> str:
    items = []
    for e in entries:
        audio = "".join(f'<audio controls preload="none" src="{html.escape(a)}"></audio>' for a in e["mp3"])
        paras = "".join(f"<p>{html.escape(p)}</p>" for p in e["body"].split("\n\n") if p.strip())
        photos = "".join(f'<img src="{html.escape(p)}" alt="" loading="lazy">' for p in e["photos"])
        q = f'<p class="q">{html.escape(e["question"])}</p>' if e["question"] else ""
        code = f'<span class="code">{html.escape(e["code"])}</span>' if e["code"] else ""
        items.append(f'<article id="{html.escape(e["code"] or e["slug"])}"><h2>{code}{html.escape(e["title"])}</h2>'
                     f'{q}{audio}{paras}{photos}</article>')
    toc = "".join(f'<li><a href="#{html.escape(e["code"] or e["slug"])}">{html.escape(e["title"])}</a></li>'
                  for e in entries)
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(project.title)}</title>
<style>body{{font-family:Georgia,serif;color:#1A2230;max-width:720px;margin:0 auto;padding:32px 20px;line-height:1.6;font-size:18px}}
h1{{font-weight:400;font-size:2.2em;margin:0 0 .2em;color:#17324D}}h2{{font-weight:600;color:#17324D;margin:2.2em 0 .3em}}
.q{{font-style:italic;color:#5B6574}}.code{{font:700 .6em system-ui,sans-serif;background:#F2B544;color:#17324D;border-radius:4px;padding:2px 6px;margin-right:10px;vertical-align:middle}}
audio{{width:100%;margin:.6em 0}}img{{max-width:100%;margin:.6em 0;border-radius:4px}}article{{border-top:1px solid #D5DDE4}}
nav li{{margin:.2em 0}}a{{color:#17324D}}</style></head><body><h1>{html.escape(project.title)}</h1>
<p>Dit is het eigen archief van de familie. Alles staat in deze map; er is geen internet nodig.</p>
<nav><ol>{toc}</ol></nav>{''.join(items)}</body></html>"""


@job("build_export", max_attempts=4)
def build_export(session: Session, payload: dict) -> None:
    export = session.get(Export, payload["export_id"])
    if export is None or export.status == "ready":
        return
    project = session.get(Project, export.project_id)
    export.status = "building"
    session.commit()
    storage = get_storage()
    when = utcnow().strftime("%d-%m-%Y %H:%M UTC")
    root = f"vertelschat-{slug(project.storyteller.name if project.storyteller else project.title)}/"
    checksums: list[str] = []
    stories_rows: list[dict] = []
    qr_rows: list[list[str]] = []
    viewer_entries: list[dict] = []
    data: dict = {"project": {"title": project.title, "locale": project.locale, "created_at": str(project.created_at),
                              "active_until": str(project.active_until or ""), "exported_at": when},
                  "storyteller": {}, "prompts": [], "stories": [], "loose_photos": []}
    st = project.storyteller
    if st:
        data["storyteller"] = {"name": st.name, "address_as": st.address_as, "birth_year": st.birth_year}
    for p in session.scalars(select(Prompt).where(Prompt.project_id == project.id).order_by(Prompt.created_at)).all():
        data["prompts"].append({"question": p.text, "status": p.status, "source": p.source,
                                "sent_at": str(p.sent_at or ""), "answered_at": str(p.answered_at or "")})
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "archief.zip"
        count = 0
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            def add_asset(asset: MediaAsset, arcname: str, compress: bool = False) -> str:
                nonlocal count
                with storage.local_path(asset.storage_key) as src:
                    z.write(src, root + arcname, compress_type=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED)
                checksums.append(f"{asset.sha256}  {arcname}")
                count += 1
                return arcname

            def add_text(arcname: str, text: str) -> None:
                nonlocal count
                raw = text.encode("utf-8")
                z.writestr(root + arcname, raw)
                checksums.append(f"{hashlib.sha256(raw).hexdigest()}  {arcname}")
                count += 1

            stories = session.scalars(select(Story).where(Story.project_id == project.id)
                                      .order_by(Story.position)).all()
            for i, story in enumerate(stories, start=1):
                qr = session.scalar(select(QRLink).where(QRLink.story_id == story.id))
                code = qr.audio_code if qr else f"V{i:02d}"
                base = f"{code}-{slug(story.title or 'verhaal')}"
                prompt = session.get(Prompt, story.prompt_id) if story.prompt_id else None
                recs = session.scalars(select(Recording).where(Recording.story_id == story.id,
                                                               Recording.retracted_at.is_(None))
                                       .order_by(Recording.received_at)).all()
                rec_meta, transcript_parts, mp3s = [], [], []
                for n, rec in enumerate(recs, start=1):
                    entry = {"kind": rec.kind, "received_at": str(rec.received_at), "status": rec.status,
                             "duration": rec.duration_seconds, "after_period": rec.after_period}
                    if rec.kind == "audio" and rec.original_media_id:
                        orig = session.get(MediaAsset, rec.original_media_id)
                        if orig and orig.status == "stored":
                            entry["original_file"] = add_asset(orig, f"audio/{base}-deel{n}{ext_for_mime(orig.mime_type)}")
                            entry["sha256"] = orig.sha256
                        pb = session.get(MediaAsset, rec.playback_media_id) if rec.playback_media_id else None
                        if pb and pb.status == "stored":
                            entry["mp3_file"] = add_asset(pb, f"audio/{base}-deel{n}.mp3")
                            mp3s.append(entry["mp3_file"])
                    tr = session.scalar(select(Transcript).where(Transcript.recording_id == rec.id))
                    if tr:
                        transcript_parts.append(f"--- deel {n} ({format_duration(rec.duration_seconds)}) ---\n{tr.text}")
                    rec_meta.append(entry)
                if transcript_parts:
                    add_text(f"transcripties/{base}.txt", "\n\n".join(transcript_parts) + "\n")
                if story.body.strip():
                    md = f"# {story.title}\n\n" + (f"*{prompt.text}*\n\n" if prompt else "") + story.body.strip() + "\n"
                    add_text(f"verhalen/{base}.md", md)
                photo_files = []
                for k, ph in enumerate(session.scalars(select(Photo).where(Photo.story_id == story.id)).all(), start=1):
                    asset = session.get(MediaAsset, ph.media_id)
                    if asset and asset.status == "stored":
                        photo_files.append(add_asset(asset, f"fotos/{base}-foto{k}{ext_for_mime(asset.mime_type)}"))
                for k, att in enumerate(session.scalars(select(MediaAsset).where(MediaAsset.story_id == story.id,
                                                                                 MediaAsset.status == "stored")).all(), 1):
                    add_asset(att, f"overige-bestanden/{base}-bijlage{k}{ext_for_mime(att.mime_type)}")
                if qr:
                    qr_rows.append([qr.audio_code, story.title, qr_url(qr.token), f"audio/{base}-deel1.mp3"])
                data["stories"].append({"code": code, "title": story.title, "question": prompt.text if prompt else "",
                                        "body": story.body, "hidden": bool(story.hidden_at),
                                        "include_in_book": story.include_in_book, "recordings": rec_meta,
                                        "photos": photo_files, "qr_url": qr_url(qr.token) if qr else ""})
                stories_rows.append({"code": code, "titel": story.title, "vraag": prompt.text if prompt else "",
                                     "delen": len(recs), "duur": format_duration(sum(r.duration_seconds or 0 for r in recs)),
                                     "ontvangen": str(story.first_part_at or "")})
                if not story.hidden_at:
                    viewer_entries.append({"code": qr.audio_code if qr else "", "slug": base, "title": story.title,
                                           "question": prompt.text if prompt else "", "body": story.body,
                                           "mp3": mp3s, "photos": photo_files})
            loose = session.scalars(select(Photo).where(Photo.project_id == project.id, Photo.story_id.is_(None))).all()
            for k, ph in enumerate(loose, start=1):
                asset = session.get(MediaAsset, ph.media_id)
                if asset and asset.status == "stored":
                    data["loose_photos"].append(add_asset(asset, f"fotos/losse-foto-{k}{ext_for_mime(asset.mime_type)}"))
            book = session.scalar(select(Book).where(Book.project_id == project.id))
            if book:
                version = session.scalar(select(BookVersion).where(BookVersion.book_id == book.id,
                                                                   BookVersion.status == "ready")
                                         .order_by(BookVersion.approved_at.is_(None), BookVersion.version.desc()))
                if version:
                    for mid, name in ((version.interior_media_id, "binnenwerk"), (version.cover_media_id, "omslag"),
                                      (version.screen_media_id, "digitale-editie")):
                        asset = session.get(MediaAsset, mid) if mid else None
                        if asset and asset.status == "stored":
                            add_asset(asset, f"boek/{slug(book.title)}-v{version.version}-{name}.pdf", compress=True)
            add_text("LEESMIJ.txt", _readme(project, when))
            add_text("index.html", _viewer(project, viewer_entries))
            add_text("projectgegevens/project.json", json.dumps(data, ensure_ascii=False, indent=2))
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["code", "titel", "vraag", "delen", "duur", "ontvangen"])
            w.writeheader()
            w.writerows(stories_rows)
            add_text("projectgegevens/verhalen.csv", buf.getvalue())
            buf = io.StringIO()
            cw = csv.writer(buf)
            cw.writerow(["audiocode", "titel", "qr_url", "bestand"])
            cw.writerows(qr_rows)
            add_text("projectgegevens/qr-codes.csv", buf.getvalue())
            z.writestr(root + "projectgegevens/CHECKSUMS-sha256.txt", "\n".join(checksums) + "\n")
        asset = store_path(session, zpath, kind="export", mime="application/zip", project_id=project.id,
                           source="generated", original_filename=f"{root.rstrip('/')}.zip")
    export.media_id = asset.id
    export.size_bytes = asset.size_bytes
    export.file_count = count
    export.status = "ready"
    export.completed_at = utcnow()
    track(session, "full_archive_exported", project.id, count=count)
    notify_organizers(session, project, "export_ready", "Je archief staat klaar",
                      f"Alle verhalen, opnames en foto's in één zip-bestand ({count} bestanden). Bewaar een kopie op "
                      "je eigen computer.", url=f"/p/{project.id}/downloads")
    older = session.scalars(select(Export).where(Export.project_id == project.id, Export.status == "ready",
                                                 Export.id != export.id).order_by(Export.completed_at.desc())).all()
    for old in older[KEEP_EXPORTS - 1:]:
        media = session.get(MediaAsset, old.media_id) if old.media_id else None
        if media and media.status == "stored":
            storage.delete(media.storage_key)
            media.status = "purged"
        old.status = "superseded"
