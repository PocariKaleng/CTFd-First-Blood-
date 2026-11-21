# CTFd First Blood Bot

Bot kecil untuk lomba CTF yang memakai **CTFd**. Bot ini memantau CTFd lalu otomatis mengirim pengumuman ke **Discord**:

- 🩸 **First Blood**: tim pertama yang berhasil solve sebuah challenge
- 🆕 **Challenge baru**: saat panitia membuka challenge baru
- 💡 **Hint baru**: saat panitia menambah hint
- 🧊 **Mode freeze**: saat scoreboard di-freeze, nama tim dan challenge di first blood disembunyikan, lalu bisa dibuka lagi setelah freeze selesai

Cukup satu file (`bot.py`), dipakai lewat menu angka. Tidak perlu hafal perintah apa pun.

## Contoh

Pesan yang muncul di Discord:

<img src="docs/discord.png" alt="Contoh pesan bot di Discord" width="560">

Menu saat bot dijalankan:

<img src="docs/menu.png" alt="Menu utama bot" width="560">

Catatan di terminal saat bot berjalan:

<img src="docs/terminal.png" alt="Log bot di terminal" width="720">

> Gambar di atas hanya contoh dengan data fiktif.

## Yang perlu disiapkan

- Python 3.8 atau lebih baru
- Akun **admin** di CTFd
- Channel Discord untuk pengumuman

## Cara pakai

**1. Download dan install**

```bash
git clone https://github.com/PocariKaleng/CTFd-First-Blood-.git
cd CTFd-First-Blood-
pip install -r requirements.txt
```

**2. Jalankan**

```bash
python bot.py
```

Saat pertama kali dijalankan, bot menanyakan pengaturan satu per satu. Setiap pertanyaan ada penjelasan dan contohnya:

| Isian | Wajib? | Keterangan |
|---|---|---|
| URL CTFd | Wajib | Alamat website CTF, contoh `https://ctf.kampus.ac.id` |
| Token admin CTFd | Wajib | Lihat [cara ambil token](#cara-ambil-token-admin-ctfd) |
| Webhook utama | Wajib | Lihat [cara bikin webhook](#cara-bikin-webhook-discord) |
| Webhook First Blood / Challenge / Hint | Tidak | Isi hanya kalau mau dikirim ke channel berbeda |
| Interval | Tidak | Seberapa sering bot mengecek CTFd (bawaan 15 detik) |
| Tampilkan isi hint | Tidak | Bawaan: tidak |

Yang tidak wajib cukup dilewati dengan menekan **Enter**. Semua isian bisa diubah lagi nanti lewat **menu 3**.

<img src="docs/setup.png" alt="Contoh proses pengisian pengaturan" width="620">

**3. Tes, lalu start**

Setelah pengisian selesai, bot menawarkan tes koneksi. Jawab **Enter** (ya): bot akan mengecek CTFd dan mengirim pesan tes ke Discord. Kalau ada yang gagal, bot memberi tahu bagian mana yang perlu diperbaiki. Tes ini juga bisa diulang kapan saja lewat **menu 5**.

Kalau semua OK, pilih **1** untuk mulai. Tekan `Ctrl+C` untuk berhenti.

Saat start pertama kali, bot bertanya *"Announce data yang sudah ada?"*. Tekan **Enter** saja supaya challenge dan solve lama tidak dikirim ulang ke Discord.

### Cara ambil token admin CTFd

1. Login ke CTFd pakai akun admin.
2. Buka **Settings → Access Tokens**.
3. Klik **Generate**, lalu salin tokennya. Token hanya muncul sekali.

### Cara bikin webhook Discord

1. Di Discord, klik ikon gerigi di channel tujuan (**Edit Channel**).
2. Buka **Integrations → Webhooks → New Webhook**.
3. Klik **Copy Webhook URL**.

## Isi menu

| No | Fungsi |
|---|---|
| 1 | Start bot |
| 2 | Start bot versi coba-coba: pesan hanya muncul di terminal, tidak dikirim ke Discord |
| 3 | Ubah pengaturan (URL, token, webhook, interval, dll.) |
| 4 | Lihat pengaturan sekarang (token dan webhook disensor) |
| 5 | Tes koneksi ke CTFd dan Discord |
| 6 | Nyalakan/matikan First Blood, Challenge Baru, atau Hint Baru |
| 7 | Kirim pengumuman bebas ke Discord |
| 8 | Reset catatan bot |
| 9 | Menu freeze |
| 0 | Keluar |

## Mode freeze

- Bot otomatis membaca waktu freeze dari CTFd. Kalau perlu, waktu freeze bisa diisi manual lewat **menu 9**.
- First blood yang terjadi **setelah** freeze dikirim tanpa nama tim dan tanpa nama challenge.
- Data aslinya tetap disimpan. Setelah freeze selesai, buka **menu 9 → 6** untuk mengumumkan semuanya sekaligus, atau nyalakan **auto-reveal** di **menu 9 → 4** supaya dikirim otomatis.

## Menjalankan bot terus-menerus di server

Isi pengaturan dulu dengan `python bot.py`, lalu jalankan dengan `--run` supaya bot langsung start tanpa menu:

```bash
nohup python bot.py --run > bot.log 2>&1 &
```

Atau pakai `screen`:

```bash
screen -S ctfbot
python bot.py --run
# keluar dari screen tanpa mematikan bot: Ctrl+A lalu D
```

## Penting

- `config.json` berisi token dan webhook. **Jangan dibagikan atau di-upload.** File ini sudah dikecualikan lewat `.gitignore`.
- `state.json` berisi catatan bot, termasuk nama tim yang disembunyikan saat freeze. Jangan dibagikan selama freeze.
- Challenge yang punya *prerequisite* (harus solve challenge lain dulu) tidak terdeteksi oleh bot.
- Kalau opsi "tampilkan isi hint" dinyalakan, hint berbayar ikut terbaca gratis di Discord.
