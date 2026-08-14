#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_fractalsv.py — Detecta nuevos proyectos/imágenes en C:\\FRACTALSV\\proyectos\\,
procesa imágenes (resize/convertir) y extrae posters de videos, actualiza el manifest
y emite un reporte JSON para que el agente del cron actualice projects.js y despliegue.

NO despliega, NO edita projects.js. Solo detecta + procesa + reporta.
"""
import os
import re
import json
import shutil
import subprocess
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "proyectos"
IMG = BASE / "img"
MANIFEST = BASE / "projects_manifest.json"
CATEGORIES = {"PROYECTOS INTERNACIONALES": "internacional",
              "PROYECTOS NACIONALES": "nacional"}
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
IGNORE_FOLDERS = {"imagenes de relleno web"}

MAX_W = 1200
JPEG_Q = 85

FFMPEG = shutil.which("ffmpeg") or \
    r"C:\Users\Diseño\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"


def slugify(text):
    t = unicodedata.normalize("NFKD", text)
    t = t.encode("ascii", "ignore").decode("ascii")
    t = t.lower().strip()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t or "proyecto"


def load_manifest():
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"mapping": {}, "files": {}}


def save_manifest(m):
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def process_image(src: Path, dst: Path):
    from PIL import Image
    dst.parent.mkdir(parents=True, exist_ok=True)
    im = Image.open(src)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        bg.alpha_composite(im)
        im = bg.convert("RGB")
    else:
        im = im.convert("RGB")
    if im.width > MAX_W:
        h = round(im.height * MAX_W / im.width)
        im = im.resize((MAX_W, h), Image.LANCZOS)
    im.save(dst, "JPEG", quality=JPEG_Q)


def process_video(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-i", str(src), "-vframes", "1", "-q:v", "3", str(dst)]
    subprocess.run(cmd, capture_output=True, timeout=120)


def existing_numbers(slug_dir: Path):
    nums = []
    if slug_dir.exists():
        for f in slug_dir.glob("*.jpg"):
            m = re.fullmatch(r"(\d+)\.jpg", f.name)
            if m:
                nums.append(int(m.group(1)))
    return nums


def next_image_name(slug_dir: Path):
    nums = existing_numbers(slug_dir)
    n = max(nums) + 1 if nums else 1
    return f"{n:02d}.jpg"


def next_poster_name(slug_dir: Path):
    if not (slug_dir / "poster.jpg").exists():
        return "poster.jpg"
    k = 2
    while (slug_dir / f"poster{k}.jpg").exists():
        k += 1
    return f"poster{k}.jpg"


def project_folder(rel_parts):
    """Devuelve (category_key, folder) o None. 'folder' es el parent completo
    (incluye la categoría) para que coincida con las claves del mapping."""
    if not rel_parts or rel_parts[0] not in CATEGORIES:
        return None, None
    if rel_parts[0] in IGNORE_FOLDERS:
        return None, None
    cat = CATEGORIES[rel_parts[0]]  # "internacional" o "nacional"
    folder = "/".join(rel_parts[:-1])  # parent completo (con categoría)
    return cat, folder


def folder_short(folder):
    """Quita la categoría del inicio: 'CAT/FOLDER' -> 'FOLDER'."""
    return folder.split("/", 1)[1] if "/" in folder else folder


def main():
    manifest = load_manifest()
    bootstrap = not manifest.get("files")

    report = {"changed": False, "bootstrap": bootstrap,
              "new_projects": [], "updated_projects": []}
    new_files = {}

    if not SRC.exists():
        print(json.dumps(report, ensure_ascii=False))
        return

    # escanear archivos media
    discovered = []  # (rel_path, is_video, category, folder)
    for root, dirs, files in os.walk(SRC):
        # saltar carpeta ignorada
        dirs[:] = [d for d in dirs if d not in IGNORE_FOLDERS]
        for name in files:
            ext = Path(name).suffix.lower()
            if ext not in MEDIA_EXT and ext not in VIDEO_EXT:
                continue
            full = Path(root) / name
            rel = full.relative_to(SRC).as_posix()
            parts = rel.split("/")
            cat, folder = project_folder(parts)
            if cat is None:
                continue
            is_video = ext in VIDEO_EXT
            discovered.append((rel, is_video, cat, folder))

    if bootstrap:
        # primer run: marcar TODO lo existente como cubierto, sin procesar
        for rel, is_video, cat, folder in discovered:
            slug = manifest["mapping"].get(folder) or slugify(folder_short(folder))
            manifest["files"][rel] = f"{slug}/@covered"
        save_manifest(manifest)
        report["changed"] = False
        print(json.dumps(report, ensure_ascii=False))
        return

    # procesar nuevos
    for rel, is_video, cat, folder in discovered:
        if rel in manifest["files"]:
            continue
        slug = manifest["mapping"].get(folder)
        is_new_proj = slug is None
        if slug is None:
            slug = slugify(folder_short(folder))
            manifest["mapping"][folder] = slug
        src_path = SRC / rel
        slug_dir = IMG / slug
        try:
            if is_video:
                out_name = next_poster_name(slug_dir)
                out = slug_dir / out_name
                process_video(src_path, out)
            else:
                out_name = next_image_name(slug_dir)
                out = slug_dir / out_name
                process_image(src_path, out)
        except Exception as e:
            report.setdefault("errors", []).append({"file": rel, "error": str(e)})
            continue
        manifest["files"][rel] = f"{slug}/{out_name}"
        new_files[slug] = new_files.get(slug, [])
        new_files[slug].append(out_name)
        if is_new_proj:
            # registrarlo en new_projects (una vez)
            if slug not in [p["slug"] for p in report["new_projects"]]:
                report["new_projects"].append(
                    {"folder": folder, "slug": slug, "category": cat})
            else:
                report["updated_projects"].append(
                    {"slug": slug, "added_images": [out_name]})
        else:
            report["updated_projects"].append(
                {"slug": slug, "added_images": [out_name]})

    # consolidar updated_projects (agrupar por slug)
    upd = {}
    for u in report["updated_projects"]:
        upd.setdefault(u["slug"], []).extend(u["added_images"])
    report["updated_projects"] = [
        {"slug": s, "added_images": imgs} for s, imgs in upd.items()]

    # añadir las imágenes nuevas de proyectos nuevos a su entrada
    for p in report["new_projects"]:
        p["images"] = new_files.get(p["slug"], [])

    if report["new_projects"] or report["updated_projects"] or report.get("errors"):
        report["changed"] = True

    save_manifest(manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
