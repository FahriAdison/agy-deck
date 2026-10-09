# agy/deck v3 — Terminal Deck

Web gateway mobile-first untuk **Antigravity CLI (`agy`)**. Bukan chatbot generik:
sesi terminal yang bisa berpikir — prompt `$`, output agent sebagai blok status,
tool call sebagai baris `→`, thinking sebagai `; thinking…`, semua dalam
amber phosphor di atas deep ink.

## Login: Google atau API key

Dua cara pakai akun Googlemu sendiri (dibutuhkan agar bukan mode mock):

**A. Login dengan Google (disarankan, apalagi kalau langganan AI Pro)**
- Buka `key:` di topbar (atau ketik `/key`) → **login_dengan_google_**
- Buka link yang muncul di browser HP-mu → login pakai akun Googlemu
- Kembali ke aplikasi → tekan **cek — aku sudah login**
- Kalau browser tidak redirect otomatis, pakai **tempel kode** (paste kode
  otorisasi dari Google)
- `agy` lalu jalan sebagai akun Googlemu langsung (termasuk jatah AI Pro
  di sisi agy). Indikator topbar jadi `google:✓`.

**B. API key** — dari **aistudio.google.com** → Get API key → tempel di sheet
yang sama → `save_key_`. Jalur ini lewat API AI Studio (terpisah dari
langganan AI Pro).

Google login diprioritaskan kalau dua-duanya ada.

## Deploy gratis: Render (disarankan)

HP kentang / tanpa VPS? Jalankan di cloud gratis, dapat URL publik
`https://agy-deck.onrender.com`. Tanpa kartu kredit.

1. Upload isi zip ini ke repo GitHub baru (di github.com → New repository →
   **Add file → Upload files**, drag & drop — nggak perlu git CLI).
2. Bikin akun di **render.com** (sign up pakai GitHub, gratis).
3. **New + → Web Service** → pilih repo tadi.
   - Kalau pakai Blueprint: **New + → Blueprint** → pilih repo (baca
     `render.yaml` otomatis).
   - Kalau manual: Runtime **Docker**, Instance type **Free**.
4. Di **Environment** → tambah variable:
   - `AGY_WEB_TOKEN` → token login pilihanmu (string acak yang panjang)
5. **Deploy** → tunggu build ±5 menit → buka URL-nya → login pakai tokenmu
   → **login dengan Google** dari dalam aplikasi (tombol `key:`).

Catatan jujur:
- Free tier tidur setelah 15 menit tidak dipakai; bangun sendiri ±30–50 detik
  saat URL dibuka (aplikasi auto-reconnect). Biar jarang tidur, bisa pasang
  ping gratis tiap 10 menit (mis. UptimeRobot) ke `https://…/api/health`.
- RAM 512 MB — cukup untuk pemakaian ringan; kalau agy OOM, berarti butuh
  tier lebih besar.
- File workspace & login Google tidak persisten antar *redeploy* — cukup
  untuk tes & coding ringan. Butuh persisten serius → VPS.

> Hugging Face Spaces dulunya opsi ini, tapi per 2026 SDK Docker/Gradio-nya
> sudah berbayar (hanya Static yang gratis) — jadi pakai Render.

## Jalankan lokal

```bash
cp .env.example .env     # isi GEMINI_API_KEY (lihat bawah)
./start.sh               # PORT=3000 ./start.sh  |  HOST=0.0.0.0 ./start.sh
./stop.sh
```

Buka `http://127.0.0.1:8080`, tempel **auth token** yang dicetak di log
(`server.log` / file `.auth_token`). Token tersimpan di browser HP.

## Auth: pakai akun Google kamu sendiri

`agy` headless butuh kredensial. Cara paling bersih: API key dari akun
Google-mu sendiri (gratis):

1. Buka https://aistudio.google.com → **Get API key** → buat key baru
2. Masukkan ke `.env`: `GEMINI_API_KEY=...`
3. Restart server. `agy` jalan dengan `modelProvider: gemini` + key milikmu —
   kuota & billing atas akunmu, bukan akun orang lain.

Key tidak pernah dikirim ke mana pun kecuali sebagai env var ke proses
`agy` lokal. Jangan commit `.env`.

## Yang berubah dari v2 (perombakan besar)

**Beneran work:**
- **Stop beneran** — abort membunuh seluruh process tree agy (`killpg`),
  bukan cuma menutup stream. Kuota berhenti terbakar.
- **`--add-dir` selalu dikirim** — file hasil kerja AI tidak nyasar ke scratch dir.
- **Sesi persisten** — satu proses `agy --input-format stream-json` per
  percakapan, bukan spawn per pesan. Thinking & tool event live.
- **Timeout + watchdog** per turn (default 600 dtk).
- **Terminal PTY sungguhan** (xterm.js) — `vim`, `ssh`, build panjang, semua jalan.
- **Token auth** + default bind `127.0.0.1` (v2: tanpa auth di `0.0.0.0`).
- **Model list dinamis** dari `agy models` (fallback ke daftar statis).
- **Script start/stop diperbaiki** (bug cek PID `$1`→`ps -p`).

**UI Terminal Deck:**
- Boot sequence, transkrip sesi ala terminal, slash palette (`/`),
  approval card eksperimental, PWA installable, CodeMirror editor +
  deteksi konflik simpanan (409 bila file berubah di luar),
  markdown disanitasi (DOMPurify), render incremental tanpa jank.

**Mode mock** — tanpa binary `agy` (atau `AGY_MOCK=1`), server jalan dengan
CLI simulasi: seluruh UI bisa dites end-to-end termasuk demo approval card.

## Approval card (eksperimental)

Mode `review` (badge yolo → review) menjalankan agy *tanpa*
`--dangerously-skip-permissions` dan menunggu event persetujuan dari CLI.
Skema event persetujuan antar versi agy belum terdokumentasi publik —
server mendeteksi event berbentuk approval apa pun dan menampilkannya
sebagai kartu ALLOW/DENY; event tak dikenal diteruskan sebagai `// log`
agar skemanya bisa di-capture dan di-wire permanen.

## Struktur

```
server/
  config.py   env, workspace, token, .env loader
  agy.py      session manager: persistent / oneshot / mock + normalisasi event
  terminal.py PTY per koneksi
  store.py    file ops (jail + mtime check) & riwayat agy (read-only)
  main.py     FastAPI: REST + WebSocket /ws
static/       UI Terminal Deck (vanilla JS, no build step)
```

## Roadmap v3.1

Voice input/output, image paste ke chat, multi-sesi paralel, diff view
hasil kerja agent, notifikasi turn selesai.
