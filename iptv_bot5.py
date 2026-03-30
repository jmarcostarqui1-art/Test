"""
IPTV Link Checker + Search Bot para Telegram

Flujo de búsqueda:
  1. El bot (o alguien) manda un mensaje con una URL Xtream
  2. Tú RESPONDES ese mensaje con:  /buscar ESPN
  3. El bot extrae las credenciales del mensaje citado y busca

También verifica suscripciones al pegar cualquier URL/credenciales.
"""

import re
import os
import requests
import logging
from urllib.parse import urlparse, parse_qs
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────
BOT_TOKEN = "TU_TOKEN_AQUI"
MAX_INLINE_RESULTS = 15   # Si hay más resultados se manda archivo .m3u
# ─────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
#  PARSERS
# ══════════════════════════════════════════

def parse_xtream_url(url: str) -> dict | None:
    """Extrae credenciales de una URL Xtream Codes."""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        username = qs.get("username", [None])[0]
        password = qs.get("password", [None])[0]
        if not username or not password:
            return None
        port = parsed.port or 80
        return {
            "host": parsed.hostname,
            "scheme": parsed.scheme,
            "port": port,
            "username": username,
            "password": password,
            "full_host": f"{parsed.scheme}://{parsed.hostname}:{port}",
        }
    except Exception:
        return None


def parse_xtream_text(text: str) -> dict | None:
    """Extrae credenciales de texto libre (Host:/Username:/Password:)."""
    host     = re.search(r"[Hh]ost[:\s]+(\S+)", text)
    port     = re.search(r"[Pp]ort[:\s]+(\d+)", text)
    username = re.search(r"[Uu]sername[:\s]+(\S+)", text)
    password = re.search(r"[Pp]assword[:\s]+(\S+)", text)
    if host and username and password:
        h = host.group(1).rstrip("/")
        p = port.group(1) if port else "80"
        if not h.startswith("http"):
            h = f"http://{h}"
        return {
            "host": urlparse(h).hostname,
            "scheme": urlparse(h).scheme,
            "port": int(p),
            "username": username.group(1),
            "password": password.group(1),
            "full_host": f"{h}:{p}",
        }
    return None


def extract_creds_from_text(text: str) -> dict | None:
    """Intenta extraer credenciales Xtream de cualquier texto."""
    # Buscar URL con usuario/contraseña
    url_match = re.search(r"https?://\S+", text)
    if url_match:
        creds = parse_xtream_url(url_match.group(0))
        if creds:
            return creds
    # Buscar formato Host:/Username:/Password:
    return parse_xtream_text(text)


def is_m3u_url(url: str) -> bool:
    low = url.lower()
    return (
        low.endswith((".m3u", ".m3u8"))
        or "type=m3u" in low
        or "type=m3u_plus" in low
    )


# ══════════════════════════════════════════
#  VERIFICADOR XTREAM
# ══════════════════════════════════════════

def check_xtream(creds: dict) -> dict:
    u    = creds["username"]
    p    = creds["password"]
    base = creds["full_host"]

    try:
        r = requests.get(
            f"{base}/player_api.php?username={u}&password={p}",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        user_info   = data.get("user_info", {})
        server_info = data.get("server_info", {})

        import datetime
        def ts(t):
            try:
                return datetime.datetime.fromtimestamp(int(t)).strftime("%d %b %Y — %I:%M %p")
            except Exception:
                return "N/A"

        status      = "✅ Activa" if user_info.get("status") == "Active" else "❌ Inactiva"
        is_trial    = "Sí" if str(user_info.get("is_trial", "0")) == "1" else "No"
        active_cons = user_info.get("active_cons", 0)
        max_cons    = user_info.get("max_connections", 0)

        def count_items(action):
            try:
                resp = requests.get(
                    f"{base}/player_api.php?username={u}&password={p}&action={action}",
                    timeout=10,
                )
                items = resp.json()
                return len(items) if isinstance(items, list) else 0
            except Exception:
                return 0

        live_count = count_items("get_live_streams")
        vod_count  = count_items("get_vod_streams")
        total      = live_count + vod_count

        links = (
            f"[mpeg]({base}/get.php?username={u}&password={p}&type=m3u) | "
            f"[m3u+]({base}/get.php?username={u}&password={p}&type=m3u_plus) | "
            f"[HLS]({base}/get.php?username={u}&password={p}&type=m3u&output=hls)"
        )

        return {
            "ok": True,
            "status": status,
            "exp_date": ts(user_info.get("exp_date")),
            "created_at": ts(user_info.get("created_at")),
            "is_trial": is_trial,
            "active_cons": active_cons,
            "max_cons": max_cons,
            "timezone": server_info.get("timezone", "N/A"),
            "host": creds["host"],
            "port": creds["port"],
            "username": u,
            "password": p,
            "links": links,
            "total_items": total,
            "live_streams": live_count,
            "vod_streams": vod_count,
        }

    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "No se pudo conectar al servidor."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "El servidor tardó demasiado en responder."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_m3u(url: str) -> dict:
    try:
        r = requests.get(url, timeout=15, stream=True)
        r.raise_for_status()
        content = b""
        for chunk in r.iter_content(8192):
            content += chunk
            if len(content) > 50_000:
                break
        text = content.decode("utf-8", errors="ignore")
        if "#EXTM3U" not in text:
            return {"ok": False, "error": "El archivo no parece un M3U válido."}
        entries = text.count("#EXTINF")
        size_label = f"≥{entries}" if len(content) >= 50_000 else str(entries)
        return {"ok": True, "entries": size_label, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════
#  BUSCADORES
# ══════════════════════════════════════════

def _fetch_list(creds: dict, action: str) -> list:
    """Descarga cualquier lista de la API Xtream."""
    u, p, base = creds["username"], creds["password"], creds["full_host"]
    try:
        r = requests.get(
            f"{base}/player_api.php?username={u}&password={p}&action={action}",
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def search_live_channels(creds: dict, query: str) -> list:
    """Busca canales en vivo."""
    u, p, base = creds["username"], creds["password"], creds["full_host"]
    query_lower = query.lower()
    results = []
    for stream in _fetch_list(creds, "get_live_streams"):
        name = stream.get("name", "")
        if query_lower in name.lower():
            sid = stream.get("stream_id")
            results.append({
                "name":      name,
                "stream_id": sid,
                "ts_url":    f"{base}/live/{u}/{p}/{sid}.ts",
                "m3u8_url":  f"{base}/live/{u}/{p}/{sid}.m3u8",
            })
    return results


def search_vod(creds: dict, query: str) -> list:
    """Busca películas (VOD)."""
    u, p, base = creds["username"], creds["password"], creds["full_host"]
    query_lower = query.lower()
    results = []
    for movie in _fetch_list(creds, "get_vod_streams"):
        name = movie.get("name", "")
        if query_lower in name.lower():
            sid = movie.get("stream_id")
            ext = movie.get("container_extension", "mp4")
            results.append({
                "name":      name,
                "stream_id": sid,
                "url":       f"{base}/movie/{u}/{p}/{sid}.{ext}",
                "year":      movie.get("year", ""),
                "rating":    movie.get("rating", ""),
            })
    return results


def search_series(creds: dict, query: str) -> list:
    """Busca series."""
    query_lower = query.lower()
    results = []
    for serie in _fetch_list(creds, "get_series"):
        name = serie.get("name", "")
        if query_lower in name.lower():
            results.append({
                "name":      name,
                "series_id": serie.get("series_id"),
                "year":      serie.get("year", ""),
                "rating":    serie.get("rating", ""),
            })
    return results


def build_m3u(results: list, query: str) -> str:
    """Genera M3U para canales en vivo."""
    lines = [f"#EXTM3U\n# Busqueda: {query}\n"]
    for ch in results:
        lines.append(f'#EXTINF:-1 tvg-id="{ch["stream_id"]}" ,{ch["name"]}')
        lines.append(ch["m3u8_url"])
    return "\n".join(lines)


def build_m3u_vod(results: list, query: str) -> str:
    """Genera M3U para películas."""
    lines = [f"#EXTM3U\n# Peliculas: {query}\n"]
    for m in results:
        lines.append(f'#EXTINF:-1 ,{m["name"]}')
        lines.append(m["url"])
    return "\n".join(lines)


# ══════════════════════════════════════════
#  FORMATTERS
# ══════════════════════════════════════════

def escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def format_xtream_result(info: dict) -> str:
    return (
        "🤖 *Subscription Info*\n"
        f"🆙 *Status:* {info['status']}\n"
        f"⏰ *Expiration:* {info['exp_date']}\n"
        f"📅 *Created:* {info['created_at']}\n"
        f"🔗 *Connections:* {info['active_cons']} / {info['max_cons']}\n"
        f"🔧 *Is Trial:* {info['is_trial']}\n"
        f"🕐 *Timezone:* {info['timezone']}\n"
        f"📦 *Total Items:* {info['total_items']}\n"
        f"📺 *Live Streams:* {info['live_streams']}\n"
        f"🎬 *On Demand (VOD):* {info['vod_streams']}\n\n"
        f"🌐 *Host:* `{info['host']}`\n"
        f"🔌 *Port:* `{info['port']}`\n"
        f"👤 *Username:* `{info['username']}`\n"
        f"🔑 *Password:* `{info['password']}`\n\n"
        f"🔗 *Links:* {info['links']}"
    )


def format_m3u_result(info: dict) -> str:
    return (
        "📋 *M3U Link Info*\n"
        f"✅ *Status:* Accesible\n"
        f"📺 *Entradas encontradas:* `{info['entries']}`\n\n"
        f"🔗 *URL:* `{info['url']}`"
    )


def format_channel_list(results: list, query: str) -> str:
    lines = [f"📺 *Resultados para:* `{escape_md(query)}` — {len(results)} encontrados\n"]
    for i, ch in enumerate(results, 1):
        lines.append(
            f"*{i}\\. {escape_md(ch['name'])}*\n"
            f"   🔴 TS: `{ch['ts_url']}`\n"
            f"   📡 HLS: `{ch['m3u8_url']}`"
        )
    return "\n".join(lines)


def format_vod_list(results: list, query: str) -> str:
    lines = [f"🎬 *Películas para:* `{escape_md(query)}` — {len(results)} encontradas\n"]
    for i, m in enumerate(results, 1):
        year    = f" \\({escape_md(str(m['year']))}\\)" if m.get("year") else ""
        rating  = f" ⭐ {escape_md(str(m['rating']))}" if m.get("rating") else ""
        lines.append(
            f"*{i}\\. {escape_md(m['name'])}{year}{rating}*\n"
            f"   🔗 `{m['url']}`"
        )
    return "\n".join(lines)


def format_series_list(results: list, query: str) -> str:
    lines = [f"📂 *Series para:* `{escape_md(query)}` — {len(results)} encontradas\n"]
    for i, s in enumerate(results, 1):
        year   = f" \\({escape_md(str(s['year']))}\\)" if s.get("year") else ""
        rating = f" ⭐ {escape_md(str(s['rating']))}" if s.get("rating") else ""
        lines.append(
            f"*{i}\\. {escape_md(s['name'])}{year}{rating}*\n"
            f"   🆔 Serie ID: `{s['series_id']}`"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *IPTV Bot* — Cómo usarlo:\n\n"
        "1️⃣ Pega una URL Xtream o credenciales → el bot verifica la suscripción\n\n"
        "2️⃣ *Para buscar, responde el mensaje con la URL y escribe:*\n"
        "   📺 `/buscar ESPN` — canales en vivo\n"
        "   🎬 `/buscarp Avengers` — películas\n"
        "   📂 `/buscars Breaking Bad` — series\n"
        "   🗂 `/categorias` — todas las categorías\n\n"
        "Usa /help para más info.",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Ayuda*\n\n"
        "*Verificar suscripción:*\n"
        "Pega la URL Xtream o el bloque Host/Username/Password directamente\n\n"
        "*Buscar contenido:*\n"
        "Responde el mensaje con la URL Xtream y usa uno de estos comandos:\n\n"
        "📺 `/buscar ESPN` — canales en vivo\n"
        "🎬 `/buscarp Avengers` — películas \\(VOD\\)\n"
        "📂 `/buscars Breaking Bad` — series\n"
        "🗂 `/categorias` — lista todas las categorías del servidor\n\n"
        "*Resultados:*\n"
        "• Hasta 15 coincidencias → se muestran en el chat con sus links\n"
        "• Más de 15 → se envía un archivo `.m3u` para importar en VLC/TiviMate",
        parse_mode="MarkdownV2",
    )


def get_all_categories(creds: dict) -> dict:
    """Obtiene todas las categorías de live, VOD y series."""
    def fetch_cats(action):
        try:
            u, p, base = creds["username"], creds["password"], creds["full_host"]
            r = requests.get(
                f"{base}/player_api.php?username={u}&password={p}&action={action}",
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            return [c.get("category_name", "?") for c in data] if isinstance(data, list) else []
        except Exception:
            return []

    return {
        "live":   fetch_cats("get_live_categories"),
        "vod":    fetch_cats("get_vod_categories"),
        "series": fetch_cats("get_series_categories"),
    }


def format_categories(cats: dict) -> list:
    """Genera lista de mensajes con todas las categorías, una sección a la vez."""
    sections = [
        ("📺 Canales en Vivo", cats["live"]),
        ("🎬 Películas \\(VOD\\)", cats["vod"]),
        ("📂 Series",             cats["series"]),
    ]
    messages = []
    for title, items in sections:
        if not items:
            continue
        # Encabezado de sección
        header = f"*{title} — {len(items)} categorías*\n"
        current = [header]
        cur_len = len(header)
        for i, name in enumerate(items, 1):
            line = f"{i}\\. {escape_md(name)}\n"
            if cur_len + len(line) > 3800:
                messages.append("".join(current))
                current = [line]
                cur_len = len(line)
            else:
                current.append(line)
                cur_len += len(line)
        if current:
            messages.append("".join(current))
    return messages


async def categorias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra todas las categorías del servidor (respondiendo mensaje con URL)."""
    replied = update.message.reply_to_message
    if not replied or not replied.text:
        await update.message.reply_text(
            "⚠️ Debes *responder* el mensaje que contiene la URL Xtream\\.\n\n"
            "Selecciona ese mensaje, respóndelo y escribe `/categorias`",
            parse_mode="MarkdownV2",
        )
        return

    creds = extract_creds_from_text(replied.text)
    if not creds:
        await update.message.reply_text(
            "❌ No encontré credenciales Xtream en el mensaje citado\\.\n"
            "Asegúrate de responder un mensaje con una URL que tenga `username=` y `password=`",
            parse_mode="MarkdownV2",
        )
        return

    msg = await update.message.reply_text(
        f"🔄 Obteniendo categorías de `{creds['host']}`...",
        parse_mode="Markdown",
    )

    cats = get_all_categories(creds)
    total = len(cats["live"]) + len(cats["vod"]) + len(cats["series"])

    if total == 0:
        await msg.edit_text("😕 No se encontraron categorías en este servidor\\.", parse_mode="MarkdownV2")
        return

    await msg.edit_text(
        f"✅ *{total} categorías encontradas*\n"
        f"📺 Live: {len(cats['live'])} — 🎬 VOD: {len(cats['vod'])} — 📂 Series: {len(cats['series'])}",
        parse_mode="Markdown",
    )

    for chunk in format_categories(cats):
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")


async def _send_results(update: Update, msg, results: list, query: str,
                        format_fn, build_m3u_fn, file_prefix: str, caption_emoji: str):
    """Función genérica: manda resultados en chat o como archivo .m3u."""
    if len(results) <= MAX_INLINE_RESULTS:
        await msg.delete()
        text = format_fn(results, query)
        chunks, current_lines, current_len = [], [], 0
        for line in text.split("\n"):
            if current_len + len(line) + 1 > 3800:
                chunks.append("\n".join(current_lines))
                current_lines, current_len = [line], len(line)
            else:
                current_lines.append(line)
                current_len += len(line) + 1
        if current_lines:
            chunks.append("\n".join(current_lines))
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="MarkdownV2")
    else:
        await msg.edit_text(
            f"📦 *{len(results)} resultados* para `{escape_md(query)}`\\.\nGenerando archivo\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        m3u_content = build_m3u_fn(results, query)
        filename    = f"{file_prefix}_{query.replace(' ', '_')}.m3u"
        filepath    = f"/tmp/{filename}"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(m3u_content)
        await update.message.reply_document(
            document=open(filepath, "rb"),
            filename=filename,
            caption=f"{caption_emoji} *{len(results)} resultados* para `{query}`\nAbre con VLC, TiviMate o tu app IPTV.",
            parse_mode="Markdown",
        )
        await msg.delete()
        os.remove(filepath)


async def _buscar_generic(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          search_fn, format_fn, build_fn,
                          file_prefix: str, emoji: str, tipo: str):
    """Lógica común para /buscar, /buscarp y /buscars."""
    if not context.args:
        await update.message.reply_text(
            f"⚠️ Uso: responde un mensaje con la URL Xtream y escribe\n`/{file_prefix} nombre`",
            parse_mode="Markdown",
        )
        return

    query   = " ".join(context.args)
    replied = update.message.reply_to_message

    if not replied or not replied.text:
        await update.message.reply_text(
            f"⚠️ Debes *responder* el mensaje que contiene la URL Xtream\\.\n\n"
            f"Selecciona ese mensaje, respóndelo y escribe `/{file_prefix} nombre`",
            parse_mode="MarkdownV2",
        )
        return

    creds = extract_creds_from_text(replied.text)
    if not creds:
        await update.message.reply_text(
            "❌ No encontré credenciales Xtream en el mensaje citado\\.\n"
            "Asegúrate de responder un mensaje con una URL que tenga `username=` y `password=`",
            parse_mode="MarkdownV2",
        )
        return

    msg = await update.message.reply_text(
        f"{emoji} Buscando *{query}* en {tipo} de `{creds['host']}`...",
        parse_mode="Markdown",
    )

    results = search_fn(creds, query)

    if not results:
        await msg.edit_text(
            f"😕 No se encontró {tipo} con *{escape_md(query)}*\\.\nIntenta con otro término\\.",
            parse_mode="MarkdownV2",
        )
        return

    await _send_results(update, msg, results, query, format_fn, build_fn, file_prefix, emoji)


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca canales en vivo."""
    await _buscar_generic(
        update, context,
        search_fn=search_live_channels,
        format_fn=format_channel_list,
        build_fn=build_m3u,
        file_prefix="live",
        emoji="📺",
        tipo="canales en vivo",
    )


async def buscarp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca películas (VOD)."""
    await _buscar_generic(
        update, context,
        search_fn=search_vod,
        format_fn=format_vod_list,
        build_fn=build_m3u_vod,
        file_prefix="peliculas",
        emoji="🎬",
        tipo="películas",
    )


async def buscars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca series."""
    await _buscar_generic(
        update, context,
        search_fn=search_series,
        format_fn=format_series_list,
        build_fn=lambda r, q: "\n".join(
            [f"#EXTM3U\n# Series: {q}\n"] +
            [f'#EXTINF:-1 ,{s["name"]}\n# ID: {s["series_id"]}' for s in r]
        ),
        file_prefix="series",
        emoji="📂",
        tipo="series",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verifica cualquier URL Xtream o M3U pegada directamente."""
    text = update.message.text.strip()

    await update.message.reply_text("🔍 Verificando el link, espera un momento...")

    url_match = re.search(r"https?://\S+", text)
    url = url_match.group(0) if url_match else None

    if url:
        xtream = parse_xtream_url(url)
        if xtream:
            info = check_xtream(xtream)
            if info["ok"]:
                await update.message.reply_text(format_xtream_result(info), parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Error: `{info['error']}`", parse_mode="Markdown")
            return

        if is_m3u_url(url):
            info = check_m3u(url)
            if info["ok"]:
                await update.message.reply_text(format_m3u_result(info), parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ Error: `{info['error']}`", parse_mode="Markdown")
            return

    creds = parse_xtream_text(text)
    if creds:
        info = check_xtream(creds)
        if info["ok"]:
            await update.message.reply_text(format_xtream_result(info), parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Error: `{info['error']}`", parse_mode="Markdown")
        return

    await update.message.reply_text(
        "⚠️ No detecté un link válido.\n\n"
        "• Pega una URL Xtream o M3U para verificar\n"
        "• Para buscar canales: responde un mensaje con la URL y escribe `/buscar ESPN`",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("buscar", buscar))
    app.add_handler(CommandHandler("buscarp", buscarp))
    app.add_handler(CommandHandler("buscars", buscars))
    app.add_handler(CommandHandler("categorias", categorias))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot iniciado — esperando mensajes.")
    app.run_polling()


if __name__ == "__main__":
    main()
