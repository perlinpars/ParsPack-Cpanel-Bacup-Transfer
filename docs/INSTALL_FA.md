# راهنمای راه‌اندازی PARSPACK Backup روی سرور cPanel/WHM

نسخه هدف: **v1.0**  
مسیر نصب ثابت: `/root/parspack-backup`

این پلاگین بکاپ روزانه cPanel را به فضای ابری S3-سازگار پارس‌پک منتقل می‌کند. آپلود جداگانه‌ای با cron ندارد؛ همان Backup Destination سفارشی WHM کار را انجام می‌دهد.

---

## ۱) پیش‌نیازها

روی سرور (به‌عنوان `root`):

| مورد | توضیح |
|---|---|
| cPanel/WHM | AlmaLinux / Rocky / CloudLinux متداول |
| `mc` | در مسیر `/usr/local/bin/mc` |
| `python3` | سیستم (`/usr/bin/python3`) |
| `perl` | سیستم (`/usr/bin/perl`) برای transport |
| مسیر بکاپ محلی | مثلاً `/backup` یا `/MainBackup` — باید root-owned و بدون write برای group/other باشد |
| دسترسی S3 | Endpoint با `https://`، Access Key، Secret Key، نام باکت |

بررسی سریع:

```bash
which mc
mc --version
python3 --version
perl -v | head -n 2
ls -ld /backup /MainBackup 2>/dev/null
```

---

## ۲) دریافت کد روی سرور

فقط در مسیر ثابت نصب کنید:

```bash
cd /root
git clone https://github.com/perlinpars/ParsPack-Cpanel-Bacup-Transfer.git parspack-backup
# یا از تگ نسخه:
# git clone --branch v1.0 https://github.com/perlinpars/ParsPack-Cpanel-Bacup-Transfer.git parspack-backup

cd /root/parspack-backup
chmod 700 install.sh uninstall.sh upgrade.sh bin/* transport/*
```

اگر از آرشیو استفاده می‌کنید، محتویات را طوری باز کنید که ریشه پروژه دقیقاً `/root/parspack-backup` باشد.

---

## ۳) پیکربندی (بدون قرار دادن رمز در گیت)

```bash
cp -a config/parspack.conf.example config/parspack.conf
chmod 600 config/parspack.conf
chown root:root config/parspack.conf
nano config/parspack.conf   # یا ویرایشگر دیگر
```

حداقل فیلدهای اجباری:

```bash
MODE="TEST"
PARSPACK_ENDPOINT="https://YOUR-ENDPOINT"
PARSPACK_ACCESS_KEY="..."
PARSPACK_SECRET_KEY="..."
PARSPACK_BUCKET="..."
PARSPACK_PREFIX="cpanel"
TRANSPORT_PASSWORD="حداقل-۱۲-کاراکتر-منحصر-به-فرد"
LOCAL_READ_ROOTS="/backup:/var/cpanel"
LOCAL_WRITE_ROOTS="/backup:/var/cpanel"
```

نکته‌ها:

- اگر بکاپ روی `/MainBackup` است، آن را به `LOCAL_READ_ROOTS` و `LOCAL_WRITE_ROOTS` اضافه کنید:  
  `LOCAL_READ_ROOTS="/MainBackup:/backup:/var/cpanel"`
- `TRANSPORT_PASSWORD` همان رمزی است که در WHM برای Custom Destination وارد می‌کنید.
- `config/parspack.conf` و `mc-config/` هرگز در گیت commit نمی‌شوند.
- ابتدا `MODE=TEST` بگذارید تا آپلود واقعی انجام نشود.

پیشنهاد بعد از تست موفق:

```bash
MODE="PRODUCTION"
DELETE_LOCAL_AFTER_SUCCESS="1"   # اختیاری؛ بعد از تأیید مقصد
ALLOW_REMOTE_DELETE="1"          # فقط اگر می‌خواهید retention مقصد کار کند
REMOTE_KEEP_DAYS="7"             # امروز + ۷ روز قبل نگه داشته می‌شود (= ۸ فولدر روز)
```

---

## ۴) نصب

```bash
cd /root/parspack-backup
perl -c transport/parspack_transport.pl    # باید: syntax OK
./install.sh
```

`install.sh` این کارها را می‌کند:

- بررسی syntax (perl / php / bash / python)
- ساخت پوشه‌های `logs`، `state`، `reports`، `mc-config`، ...
- اجرای `parspackctl doctor`
- کپی transport به:  
  `/usr/local/cpanel/scripts/parspack_custom_transport.pl`
- کپی صفحه WHM به:  
  `/usr/local/cpanel/whostmgr/docroot/cgi/parspack_backup/index.php`
- ثبت AppConfig در WHM

سپس:

```bash
./bin/parspackctl doctor
./bin/parspackctl connection-test
```

هر دو باید بدون خطای پیکربندی تمام شوند. در `MODE=TEST` ممکن است حذف probe انجام نشود؛ مهم این است که Endpoint / TLS / Auth / Bucket سالم باشند.

---

## ۵) تنظیم Backup Destination در WHM

1. وارد **WHM → Backup → Backup Configuration** شوید.
2. بخش **Additional Destinations** را باز کنید.
3. نوع را **Custom** بگذارید.
4. مقادیر پیشنهادی:

| فیلد | مقدار |
|---|---|
| Destination Name | مثلاً `PARSPACK` |
| Script | `/usr/local/cpanel/scripts/parspack_custom_transport.pl` |
| Backup Directory | مثلاً `daily` یا خالی (زیر `PARSPACK_PREFIX`) |
| Host / User | می‌تواند placeholder باشد؛ پلاگین از conf می‌خواند |
| Password | **همان** `TRANSPORT_PASSWORD` |

5. **Validate Destination** را بزنید. باید موفق شود (فایل `validate.tmp-...` موقتی ساخته و حذف می‌شود).
6. مقصد را Enable کنید و یک Backup دستی/زمان‌بندی‌شده اجرا کنید.

صفحه پلاگین در WHM:

**WHM → Plugins → PARSPACK Backup**  
(یا مسیر CGI: `/cgi/parspack_backup/index.php`)

تب‌ها: **پیشخوان**، **گزارش‌ها**، **تنظیمات**.

---

## ۶) جریان عادی بعد از نیمه‌شب

1. cPanel بکاپ محلی را می‌سازد (`accounts/` و `system/`).
2. برای هر حساب، `put` از طریق transport صدا زده می‌شود.
3. حساب‌ها به مقصد می‌روند؛ بعد از بسته شدن مجموعه، `system` و سپس `manifest.json` ارسال می‌شوند.
4. در `PRODUCTION` اگر `ALLOW_REMOTE_DELETE=1` و `REMOTE_KEEP_DAYS>0` باشد، بعد از موفقیت روز، فولدرهای قدیمی‌تر از پنجره نگهداری حذف می‌شوند.
5. گزارش HTML/ایمیل و وضعیت در پیشخوان به‌روز می‌شود.

اگر روز `CRITICAL` شد (معمولاً `system-pending` مانده):

```bash
cd /root/parspack-backup
ls -la state/system-pending-*
# از WHM: ارسال مانده‌ها
# یا:
bin/send-remaining.sh
```

---

## ۷) بررسی سلامت

```bash
cd /root/parspack-backup

# وضعیت روز
python3 bin/report.py dashboard

# گزارش یک روز مشخص
python3 bin/report.py 2026-10-14

# پیش‌نمایش retention (حذف نمی‌کند)
bin/parspackctl retention --dry-run

# لاگ
tail -n 100 logs/backup.log
tail -n 50 logs/send-system.log
```

مقصد:

```bash
. config/parspack.conf
mc --config-dir "$MC_CONFIG_DIR" ls \
  "${MC_ALIAS}/${PARSPACK_BUCKET}/${PARSPACK_PREFIX}/"
```

---

## ۸) ارتقا به نسخه جدید

```bash
cd /root/parspack-backup
# بکاپ از conf و state را نگه دارید؛ install آن‌ها را بازنویسی نمی‌کند
git fetch --tags
git checkout v1.0   # یا نسخه جدیدتر
chmod 700 install.sh bin/* transport/*
./install.sh
./bin/parspackctl doctor
```

---

## ۹) حذف نصب

```bash
cd /root/parspack-backup
./uninstall.sh
```

`uninstall.sh` transport و ثبت WHM را برمی‌دارد. `config/parspack.conf` را معمولاً نگه می‌دارد تا خودتان حذف کنید. `mc-config` ممکن است پاک شود؛ قبل از uninstall از کلیدها پشتیبان بگیرید اگر لازم است.

---

## ۱۰) امنیت و چیزهایی که نباید در گیت باشد

| مسیر | وضعیت |
|---|---|
| `config/parspack.conf` | خصوصی — در `.gitignore` |
| `mc-config/` | خصوصی — در `.gitignore` |
| `state/`، `logs/`، `reports/` | خصوصی — در `.gitignore` |
| `config/parspack.conf.example` | عمومی — فقط مقدار نمونه `CHANGE_ME` |

هیچ Access Key / Secret / رمز واقعی را در issue، README یا commit نگذارید.

---

## عیب‌یابی کوتاه

| مشکل | اقدام |
|---|---|
| `install.sh` مسیر را رد می‌کند | باید دقیقاً `/root/parspack-backup` باشد |
| Validate در WHM شکست می‌خورد | `TRANSPORT_PASSWORD` و رمز Destination یکی نیست؛ یا `doctor`/`connection-test` را ببینید |
| `MODE=TEST` و بکاپ «موفق» محلی پاک می‌شود؟ | در TEST آپلود واقعی نیست و engine با کد ۳ خارج می‌شود تا cPanel آن را بکاپ ذخیره‌شده حساب نکند |
| وضعیت بحرانی با حساب‌های کامل | پوشه `system`/`manifest` تمام نشده؛ `send-remaining` یا لاگ `send-system` |
| retention روزها را پاک نمی‌کند | `ALLOW_REMOTE_DELETE=1`، `MODE=PRODUCTION`، `REMOTE_KEEP_DAYS>0`، و روز باید کامل شده باشد؛ اول `retention --dry-run` |
| باکت versioned و فولدر خالی می‌ماند | `remote-ops.sh` نسخه‌دار حذف می‌کند؛ در صورت نیاز دستی با `mc rm --versions` |

جزئیات بیشتر: `README.md`، `docs/CLI.md`، `docs/WHM_UI.md`، `docs/CPANEL_COMPLIANCE.md`.
