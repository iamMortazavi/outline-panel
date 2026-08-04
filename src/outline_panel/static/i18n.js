/* Shared translation layer for the dashboard, the Mini App and the subscriber
   page. One file so a string is translated once, not three times.

   `t('key')` returns the current language's text, falling back to English, then
   to the key itself — a missing entry shows something readable rather than
   blanking the UI.

   API errors are looked up by the `code` the server now sends alongside the
   English `detail`. An error with no code, or a code with no translation, shows
   `detail` — which is why the backend could be converted gradually. */
(function (global) {
  'use strict';

  var DICT = {
    en: {},   // English is the source: every t() falls through to its key text

    fa: {
      /* ---- generic ---- */
      'Copy': 'کپی', 'Copied': 'کپی شد', 'Close': 'بستن', 'Done': 'تمام',
      'Cancel': 'انصراف', 'Save': 'ذخیره', 'Delete': 'حذف', 'Edit': 'ویرایش',
      'Back': 'بازگشت', 'Retry': 'تلاش دوباره', 'Refresh': 'تازه‌سازی',
      'Loading…': 'در حال بارگذاری…', 'Search': 'جستجو', 'All': 'همه',
      'Yes': 'بله', 'No': 'خیر', 'Unlimited': 'نامحدود', 'No expiry': 'بدون انقضا',
      'Expired': 'منقضی شده', 'Active': 'فعال', 'Disabled': 'غیرفعال',
      'Pending': 'در انتظار', 'Online': 'آنلاین', 'never': 'هرگز',
      'just now': 'همین حالا', 'Server': 'سرور', 'Servers': 'سرورها',
      'Language': 'زبان',

      /* ---- login ---- */
      'Welcome back.': 'خوش آمدید.',
      'Sign in to manage your servers and keys.':
        'برای مدیریت سرورها و کلیدها وارد شوید.',
      'Username': 'نام کاربری', 'Password': 'رمز عبور',
      'Two-factor code': 'کد دو مرحله‌ای', 'Sign in →': 'ورود →',
      'Sign out': 'خروج', 'Enter your two-factor code.': 'کد دو مرحله‌ای را وارد کنید.',

      /* ---- dashboard ---- */
      'Total keys': 'کل کلیدها', 'Online now': 'آنلاین اکنون',
      'Transfer · 30d': 'مصرف · ۳۰ روز', 'Credit': 'اعتبار',
      'New key': 'کلید جدید', 'New user': 'کاربر جدید',
      'Users': 'کاربران', 'Admins': 'ادمین‌ها', 'Search keys…': 'جستجوی کلیدها…',
      'No keys here yet.': 'هنوز کلیدی اینجا نیست.',
      'No servers yet.': 'هنوز سروری اضافه نشده.',
      'Add your first server': 'اولین سرور را اضافه کنید',
      'Key': 'کلید', 'Status': 'وضعیت', 'Data usage': 'مصرف داده',
      'Devices': 'دستگاه‌ها', 'Last seen': 'آخرین اتصال',
      'Expiry / Actions': 'انقضا / عملیات', 'Details': 'جزئیات',
      'Newest': 'جدیدترین', 'Name': 'نام', 'Usage': 'مصرف',

      /* ---- settings ---- */
      'Servers & settings': 'سرورها و تنظیمات',
      'Manage your Outline servers, and configure the panel.':
        'سرورهای Outline خود را مدیریت و پنل را پیکربندی کنید.',
      'Panel': 'پنل', 'Panel settings': 'تنظیمات پنل',
      'Telegram bot': 'ربات تلگرام',
      'Sub-admins, their servers & rights': 'ادمین‌های واسطه، سرورها و دسترسی‌ها',
      'Packages': 'پکیج‌ها', 'What admins on credit may sell':
        'آنچه ادمین‌های اعتباری می‌توانند بفروشند',
      'Security': 'امنیت', 'Password & two-factor': 'رمز عبور و ورود دو مرحله‌ای',
      'Backup & restore': 'پشتیبان‌گیری و بازیابی',
      'Download or restore all data': 'دانلود یا بازیابی همه‌ی داده‌ها',
      'Activity log': 'گزارش فعالیت', 'Who did what, and when': 'چه کسی، چه کاری، چه زمانی',
      'My account': 'حساب من', 'Change your password': 'تغییر رمز عبور',
      'Change password': 'تغییر رمز عبور',
      'Current password': 'رمز عبور فعلی', 'New password': 'رمز عبور جدید',
      'Everything the panel runs on. Changes apply immediately — no restart.':
        'همه‌ی مقادیری که پنل با آن‌ها کار می‌کند. تغییرات بلافاصله اعمال می‌شود — بدون ری‌استارت.',
      'Reset all to defaults': 'بازگرداندن همه به پیش‌فرض',
      'Settings saved': 'تنظیمات ذخیره شد', 'Back to defaults': 'به پیش‌فرض بازگشت',
      'Every change made through the panel. Passwords and tokens are never stored.':
        'هر تغییری که از طریق پنل انجام شده. رمزها و توکن‌ها هرگز ذخیره نمی‌شوند.',
      'Load more': 'بیشتر', 'Nothing recorded yet.': 'هنوز چیزی ثبت نشده.',
      'not signed in': 'وارد نشده',
      'Created a user': 'کاربر ساخته شد', 'Deleted a user': 'کاربر حذف شد',
      'Extended a user': 'کاربر تمدید شد', 'Changed a data limit': 'حجم تغییر کرد',
      'Disabled a user': 'کاربر غیرفعال شد', 'Enabled a user': 'کاربر فعال شد',
      'Reset usage': 'مصرف صفر شد', 'Transferred a user': 'کاربر منتقل شد',
      'Signed in': 'ورود', 'Signed out': 'خروج',
      'Admin management': 'مدیریت ادمین‌ها', 'Package management': 'مدیریت پکیج‌ها',
      'Server management': 'مدیریت سرورها', 'Restored a backup': 'بازیابی پشتیبان',
      'Changed settings': 'تغییر تنظیمات',
      'Password changed': 'رمز عبور تغییر کرد',
      'Enter your current password.': 'رمز عبور فعلی را وارد کنید.',
      'New password must be at least 6 characters.':
        'رمز جدید باید حداقل ۶ کاراکتر باشد.',

      /* ---- subscriber page ---- */
      'SUBSCRIPTION': 'اشتراک', 'Data used': 'داده‌ی مصرف‌شده',
      'Expiry': 'انقضا', 'Validity': 'اعتبار', 'Configs': 'کانفیگ‌ها',
      'starts on first connection': 'از اولین اتصال شروع می‌شود',
      'Subscription link': 'لینک اشتراک', 'Copy link': 'کپی لینک',
      'Connect': 'اتصال', 'Add': 'افزودن', 'Open': 'باز کردن',
      'Copy config': 'کپی کانفیگ', 'Off': 'خاموش',
      'Install one of the apps below.': 'یکی از برنامه‌های زیر را نصب کنید.',
      'Press connect. Your data and expiry update automatically.':
        'دکمه‌ی اتصال را بزنید. مصرف و انقضا خودکار به‌روز می‌شوند.',
      'Official app — simplest': 'برنامه‌ی رسمی — ساده‌ترین',
      'Imports the whole subscription': 'کل اشتراک را وارد می‌کند',
      'Popular on iPhone': 'محبوب روی آیفون', 'Paid · App Store': 'پولی · App Store',
      'Widely used on Android': 'پرکاربرد روی اندروید',
      'Desktop & Android': 'دسکتاپ و اندروید', 'Cross-platform': 'چندسکویی',
      'Subscription not found': 'اشتراک پیدا نشد',
      'Could not load subscription': 'اشتراک بارگذاری نشد',
      'Check the link, or ask your provider for a new one.':
        'لینک را بررسی کنید یا از فروشنده لینک تازه بگیرید.',
      'Auto-updates in your app': 'در برنامه‌ی شما خودکار به‌روز می‌شود',
      'left': 'باقی‌مانده', 'of': 'از',
      'Unlimited plan': 'طرح نامحدود',
      /* Platform and app names are deliberately absent: iPhone, Android,
         Windows, macOS, Hiddify, Streisand are written in Latin script in
         Persian too, and "translating" them would only make them harder to
         match against what the user sees in their app store. */
      'days': 'روز', 'server': 'سرور', 'servers': 'سرور',
      'auto-updates': 'به‌روزرسانی خودکار', 'h': ' ساعت',
      'Adds the first server — use Configs below for the rest':
        'فقط سرور اول را اضافه می‌کند — بقیه را از بخش کانفیگ‌ها بردارید',
      'Tap <b>Add</b> — the app opens and imports your subscription.':
        'دکمه‌ی <b>افزودن</b> را بزنید — برنامه باز می‌شود و اشتراک را وارد می‌کند.',
      /* one literal, not a concatenation: this is a key, and the caller builds
         the same sentence in two pieces — the joined result must match exactly */
      'Nothing happened? The app probably isn’t installed yet. Install it, then copy the subscription link above and paste it in the app.':
        'اتفاقی نیفتاد؟ احتمالاً برنامه نصب نیست. نصبش کنید، بعد لینک اشتراک بالا را کپی و در برنامه جای‌گذاری کنید.',
      'App not installed yet — install it, then tap Add':
        'برنامه هنوز نصب نیست — نصبش کنید و بعد افزودن را بزنید',
      'Something went wrong': 'مشکلی پیش آمد',


      /* ---- dashboard ---- */
      'Control': 'کنترل',
      'All servers': 'همه سرورها',
      'Settings': 'تنظیمات',
      'Sign out': 'خروج',
      'New key': 'کلید جدید',
      'New user': 'کاربر جدید',
      'Table': 'جدول',
      'Cards': 'کارت',
      'Toggle table / card view': 'تغییر نمای جدول / کارت',
      'Filter ·': 'فیلتر ·',
      'Status ·': 'وضعیت ·',
      'Search keys': 'جستجوی کلیدها',
      'Clear': 'پاک کردن',
      'CSV': 'CSV',
      'Manage': 'مدیریت',
      'Show all servers': 'نمایش همه سرورها',
      'keys': 'کلید',
      'online': 'آنلاین',
      'Subscription': 'اشتراک',
      'Total keys': 'کل کلیدها',
      'Online now': 'آنلاین اکنون',
      'Active': 'فعال',
      'Pending': 'در انتظار',
      'Transfer · 30d': 'مصرف · ۳۰ روز',
      'Credit': 'اعتبار',
      'No servers yet.': 'هنوز سروری اضافه نشده.',
      'Add your first server': 'اولین سرور را اضافه کنید',
      'Nothing yet.': 'هنوز چیزی نیست.',
      'Nothing to export': 'چیزی برای خروجی نیست',
      'Details': 'جزئیات',
      'Data usage': 'مصرف داده',
      'Devices': 'دستگاه‌ها',
      'Copy': 'کپی',
      'QR': 'QR',
      'Copied': 'کپی شد',
      'Copied to clipboard': 'در کلیپ‌بورد کپی شد',
      'Could not decode this key': 'این کلید قابل خواندن نبود',
      'Retry': 'تلاش دوباره',
      'Couldn\'t load the dashboard': 'داشبورد بارگذاری نشد',
      'Loading…': 'در حال بارگذاری…',
      'DISABLED': 'غیرفعال',
      'OWNER': 'مالک',
      'LIVE': 'زنده',
      'ESC': 'ESC',
      'Create a new key': 'ساخت کلید جدید',
      'Create key': 'ساخت کلید',
      'Create user': 'ساخت کاربر',
      'Generate one Outline access key for a single user.': 'یک کلید دسترسی Outline برای یک کاربر بسازید.',
      'Name': 'نام',
      'Label': 'برچسب',
      'e.g. Mohsen — iPhone': 'مثلاً محسن — آیفون',
      'Data limit · GB': 'حجم · گیگابایت',
      'Gigabytes': 'گیگابایت',
      'Valid for': 'مدت اعتبار',
      'Days': 'روز',
      'Monthly quota · GB (0 = off)': 'سهمیه ماهانه · گیگابایت (۰ = خاموش)',
      'Auto-renew off': 'تمدید خودکار خاموش',
      'Monthly quota refreshes every 30 days.': 'سهمیه ماهانه هر ۳۰ روز تازه می‌شود.',
      'Start countdown now': 'شمارش را همین حالا شروع کن',
      'Off: the clock starts on the user\'s first connection.': 'خاموش: شمارش از اولین اتصال کاربر شروع می‌شود.',
      'On: the clock starts the moment the key is created.': 'روشن: شمارش از لحظه ساخت کلید شروع می‌شود.',
      'Leave data or days at 0 for unlimited.': 'برای نامحدود، حجم یا روز را ۰ بگذارید.',
      'Please enter a name.': 'لطفاً یک نام وارد کنید.',
      'Add a server first.': 'اول یک سرور اضافه کنید.',
      'No server available.': 'سروری در دسترس نیست.',
      'Pick a package.': 'یک پکیج انتخاب کنید.',
      'Loading packages…': 'در حال بارگذاری پکیج‌ها…',
      'No packages available.': 'پکیجی در دسترس نیست.',
      'No packages are available yet. Ask the owner to add one.': 'هنوز پکیجی وجود ندارد. از مالک بخواهید اضافه کند.',
      'Pick a package. Its price comes out of your credit — you have': 'یک پکیج انتخاب کنید. قیمتش از اعتبار شما کم می‌شود — موجودی شما',
      'Scan with the Outline app, or copy the link to share it.': 'با برنامه Outline اسکن کنید، یا لینک را کپی و ارسال کنید.',
      'Copy link': 'کپی لینک',
      'Done': 'تمام',
      'Close': 'بستن',
      'Cancel': 'انصراف',
      'Save': 'ذخیره',
      'Delete': 'حذف',
      'Edit': 'ویرایش',
      'Back': 'بازگشت',
      'Apply': 'اعمال',
      'Manage key': 'مدیریت کلید',
      'Belongs to': 'متعلق به',
      'Transfer to…': 'انتقال به…',
      'Transfer': 'انتقال',
      'Renew': 'تمدید',
      'Renew with this package': 'تمدید با این پکیج',
      'Renewed': 'تمدید شد',
      'Renewing adds the package\'s time and data. You have': 'تمدید، زمان و حجم پکیج را اضافه می‌کند. موجودی شما',
      'Validity · add or reduce': 'اعتبار · افزایش یا کاهش',
      'Days — positive extends, negative shortens': 'روز — مثبت تمدید، منفی کاهش',
      'Enter a number of days to add or reduce.': 'تعداد روز برای افزایش یا کاهش را وارد کنید.',
      'At most 3650 days at a time.': 'حداکثر ۳۶۵۰ روز در هر بار.',
      'Data limit · GB (0 = unlimited)': 'حجم · گیگابایت (۰ = نامحدود)',
      '0 = unlimited': '۰ = نامحدود',
      '0 = no expiry': '۰ = بدون انقضا',
      'Disable': 'غیرفعال کردن',
      'Enabled': 'فعال شد',
      'Reset usage': 'صفر کردن مصرف',
      'Usage reset': 'مصرف صفر شد',
      'Delete key': 'حذف کلید',
      'Key deleted': 'کلید حذف شد',
      'Extend +30d': 'تمدید +۳۰ روز',
      'Name cannot be empty.': 'نام نمی‌تواند خالی باشد.',
      'Saved': 'ذخیره شد',
      'Changes saved': 'تغییرات ذخیره شد',
      'JSON config': 'کانفیگ JSON',
      'Copy JSON': 'کپی JSON',
      'Dynamic-key config': 'کانفیگ کلید پویا',
      'QR unavailable offline.': 'QR در حالت آفلاین در دسترس نیست.',
      'Servers in this subscription': 'سرورهای این اشتراک',
      'One auto-updating link for': 'یک لینک خودتازه‌شونده برای',
      'Include another server\'s config in this one link — handy for failover in v2ray.': 'کانفیگ سرور دیگری را در همین لینک بگنجانید — برای جایگزینی خودکار در v2ray مفید است.',
      '. Clients show data &amp; expiry and refresh it automatically.': '. برنامه‌ها مصرف و انقضا را نشان می‌دهند و خودکار به‌روز می‌کنند.',
      'Servers &amp; settings': 'سرورها و تنظیمات',
      'Manage your Outline servers, and configure the panel.': 'سرورهای Outline خود را مدیریت و پنل را پیکربندی کنید.',
      '+ Add a server': '+ افزودن سرور',
      'Add a server': 'افزودن سرور',
      'Add server': 'افزودن',
      'Server connected': 'سرور متصل شد',
      'Label (shown in this panel)': 'برچسب (نمایش در این پنل)',
      'e.g. Sweden · Hetzner': 'مثلاً سوئد · Hetzner',
      'API URL or access config': 'آدرس API یا کانفیگ دسترسی',
      'Paste the API URL.': 'آدرس API را جای‌گذاری کنید.',
      'Enter a label.': 'یک برچسب وارد کنید.',
      'Connect &amp; add': 'اتصال و افزودن',
      'Connect & add': 'اتصال و افزودن',
      'Outline server name': 'نام سرور در Outline',
      'Metrics sharing': 'اشتراک‌گذاری متریک',
      'Per-server metrics': 'متریک هر سرور',
      'Metrics': 'متریک',
      'Live bandwidth, online users &amp; ISP locations.': 'پهنای باند زنده، کاربران آنلاین و موقعیت ISP.',
      'Default data limit · all keys · GB': 'حجم پیش‌فرض · همه کلیدها · گیگابایت',
      'No global limit': 'بدون محدودیت کلی',
      'Remove this server': 'حذف این سرور',
      'Server removed': 'سرور حذف شد',
      'You can see this server but not change it.': 'این سرور را می‌بینید ولی نمی‌توانید تغییرش دهید.',
      'Advanced stats are off.': 'آمار پیشرفته خاموش است.',
      'Live bandwidth': 'پهنای باند زنده',
      'Connections by network · ISP': 'اتصال‌ها بر اساس شبکه · ISP',
      'Admins': 'ادمین‌ها',
      '+ Add an admin': '+ افزودن ادمین',
      'Add an admin': 'افزودن ادمین',
      'Admin created': 'ادمین ساخته شد',
      'Admin deleted': 'ادمین حذف شد',
      'Delete admin': 'حذف ادمین',
      'Create admin': 'ساخت ادمین',
      'They sign in with this username and password.': 'با این نام کاربری و رمز وارد می‌شوند.',
      'Username': 'نام کاربری',
      'Password': 'رمز عبور',
      'e.g. sara': 'مثلاً سارا',
      'At least 6 characters': 'حداقل ۶ کاراکتر',
      'Password must be at least 6 characters.': 'رمز باید حداقل ۶ کاراکتر باشد.',
      'Enter a username.': 'یک نام کاربری وارد کنید.',
      'Pick at least one server.': 'حداقل یک سرور انتخاب کنید.',
      'What they can do': 'چه کارهایی می‌تواند بکند',
      'Servers they can use': 'سرورهایی که می‌تواند استفاده کند',
      'Telegram ID': 'شناسه تلگرام',
      '(optional)': '(اختیاری)',
      'They get it by sending /id to the bot': 'با ارسال /id به ربات آن را می‌گیرد',
      'Sell from packages, on credit': 'فروش از پکیج‌ها، به‌صورت اعتباری',
      'Off: they create users freely, like today.': 'خاموش: کاربران را آزادانه می‌سازد، مثل امروز.',
      'Discount %': 'تخفیف ٪',
      'Statement': 'صورتحساب',
      'See statement': 'دیدن صورتحساب',
      'See their users': 'دیدن کاربرانش',
      'Add or correct credit · Toman': 'افزودن یا اصلاح اعتبار · تومان',
      'e.g. 500000 or -50000': 'مثلاً ۵۰۰۰۰۰ یا -۵۰۰۰۰',
      'Enter an amount to add (or a negative one to correct).': 'مبلغی برای افزودن (یا منفی برای اصلاح) وارد کنید.',
      'Credit updated': 'اعتبار به‌روز شد',
      'Disabled (signs them out immediately)': 'غیرفعال (بلافاصله خارج می‌شود)',
      'Changes apply immediately, on their next request.': 'تغییرات در درخواست بعدی‌شان اعمال می‌شود.',
      'Every change is recorded.': 'هر تغییر ثبت می‌شود.',
      'Signed in as': 'وارد شده به‌عنوان',
      'Packages': 'پکیج‌ها',
      '+ Add a package': '+ افزودن پکیج',
      'Add a package': 'افزودن پکیج',
      'Create package': 'ساخت پکیج',
      'Package added': 'پکیج اضافه شد',
      'Package deleted': 'پکیج حذف شد',
      'Delete package': 'حذف پکیج',
      'Package': 'پکیج',
      'No packages yet. Admins on credit cannot sell until you add one.': 'هنوز پکیجی نیست. ادمین‌های اعتباری تا وقتی پکیجی نسازید نمی‌توانند بفروشند.',
      'e.g. 5 GB · 1 month': 'مثلاً ۵ گیگ · ۱ ماه',
      'Data · GB (0 = unlimited)': 'حجم · گیگابایت (۰ = نامحدود)',
      'Days (0 = no expiry)': 'روز (۰ = بدون انقضا)',
      'Price · Toman': 'قیمت · تومان',
      'e.g. 30000': 'مثلاً ۳۰۰۰۰',
      'Editing the price does not change what was already sold.': 'تغییر قیمت، فروش‌های قبلی را عوض نمی‌کند.',
      'Telegram bot': 'ربات تلگرام',
      'Bot token': 'توکن ربات',
      'Test token': 'تست توکن',
      'Valid · @': 'معتبر · @',
      'Enter a token to test.': 'یک توکن برای تست وارد کنید.',
      '123456:ABC… (blank = keep current)': '123456:ABC… (خالی = بدون تغییر)',
      'Admin Telegram IDs (comma separated)': 'شناسه‌های تلگرام ادمین‌ها (با کاما)',
      'e.g. 11111111, 22222222': 'مثلاً 11111111, 22222222',
      'Send /id to your bot to find an ID.': 'برای یافتن شناسه، /id را به ربات بفرستید.',
      'Mini App URL (public HTTPS base)': 'آدرس Mini App (پایه HTTPS عمومی)',
      'Opens a Telegram Web App from the bot at': 'یک وب‌اپ تلگرام از ربات باز می‌کند در',
      '. Must be HTTPS.': '. باید HTTPS باشد.',
      'Turn the bot on or off.': 'ربات را روشن یا خاموش کنید.',
      'Save &amp; apply': 'ذخیره و اعمال',
      'Save & apply': 'ذخیره و اعمال',
      'Running': 'در حال اجرا',
      'Configured': 'پیکربندی‌شده',
      'Not configured': 'پیکربندی نشده',
      'Security': 'امنیت',
      'Security & two-factor': 'امنیت و ورود دو مرحله‌ای',
      'Two-factor authentication': 'ورود دو مرحله‌ای',
      'Your account the panel password and set up two-factor authentication.': 'حساب شما، رمز پنل و تنظیم ورود دو مرحله‌ای.',
      'Current password': 'رمز عبور فعلی',
      'New password': 'رمز عبور جدید',
      '(leave blank to keep)': '(خالی = بدون تغییر)',
      'Save changes': 'ذخیره تغییرات',
      'Nothing to change.': 'چیزی برای تغییر نیست.',
      'Password changed': 'رمز عبور تغییر کرد',
      'Enter your current password.': 'رمز عبور فعلی را وارد کنید.',
      'New password must be at least 6 characters.': 'رمز جدید باید حداقل ۶ کاراکتر باشد.',
      'Set up 2FA': 'راه‌اندازی ورود دو مرحله‌ای',
      'Enable 2FA': 'فعال‌سازی',
      'Disable 2FA': 'غیرفعال‌سازی',
      '2FA enabled': 'ورود دو مرحله‌ای فعال شد',
      '2FA disabled': 'ورود دو مرحله‌ای غیرفعال شد',
      '2FA is': 'ورود دو مرحله‌ای',
      'Password to disable': 'رمز برای غیرفعال‌سازی',
      '6-digit code': 'کد ۶ رقمی',
      'Scan with Google Authenticator / Authy, then enter the 6-digit code.': 'با Google Authenticator یا Authy اسکن کنید، سپس کد ۶ رقمی را وارد کنید.',
      'Could not load 2FA status.': 'وضعیت ورود دو مرحله‌ای بارگذاری نشد.',
      'My account': 'حساب من',
      'Change password': 'تغییر رمز عبور',
      'Backup &amp; restore': 'پشتیبان‌گیری و بازیابی',
      'Backup & restore': 'پشتیبان‌گیری و بازیابی',
      '⤓ Download backup': '⤓ دانلود پشتیبان',
      'Backup downloaded': 'پشتیبان دانلود شد',
      'Restore from file': 'بازیابی از فایل',
      'Restore': 'بازیابی',
      'Restoring': 'در حال بازیابی',
      'Restored': 'بازیابی شد',
      'all current data.': 'همه داده‌های فعلی.',
      'Choose a backup file first.': 'اول یک فایل پشتیبان انتخاب کنید.',
      'replaces': 'جایگزین می‌کند',
      'Restore will REPLACE all current data. Continue?': 'بازیابی همه داده‌های فعلی را جایگزین می‌کند. ادامه می‌دهید؟',
      'Panel': 'پنل',
      'Panel settings': 'تنظیمات پنل',
      'Settings saved': 'تنظیمات ذخیره شد',
      'Back to defaults': 'بازگشت به پیش‌فرض',
      'Reset all to defaults': 'بازگرداندن همه به پیش‌فرض',
      'Everything the panel runs on. Changes apply immediately — no restart.': 'همه مقادیری که پنل با آن‌ها کار می‌کند. تغییرات بلافاصله اعمال می‌شود — بدون ری‌استارت.',
      'Activity log': 'گزارش فعالیت',
      'Load more': 'بیشتر',
      'Nothing recorded yet.': 'هنوز چیزی ثبت نشده.',
      'Every change made through the panel. Passwords and tokens are never stored.': 'هر تغییری که از طریق پنل انجام شده. رمزها و توکن‌ها هرگز ذخیره نمی‌شوند.',
      'Keyboard shortcuts': 'میان‌برهای صفحه‌کلید',
      'Move faster across the panel.': 'سریع‌تر در پنل جابه‌جا شوید.',
      'Type a command…': 'یک فرمان بنویسید…',
      'No matching commands': 'فرمانی پیدا نشد',
      'Unlimited': 'نامحدود',
      'Server': 'سرور',
      'Search keys…': 'جستجوی کلیدها…',


      /* ---- dashboard, builder arguments ---- */
      'My credit': 'اعتبار من',
      'All statuses': 'همه وضعیت‌ها',
      'keys total': 'کلید در کل',
      'Thresholds, intervals, cache & currency': 'آستانه‌ها، بازه‌ها، کش و واحد پول',


      /* ---- tabs, sort, server picker ---- */
      'Name A–Z': 'نام الف–ی',
      'Most usage': 'بیشترین مصرف',
      'Expiring soon': 'نزدیک به انقضا',
      'Most devices': 'بیشترین دستگاه',
      'Online first': 'اول آنلاین‌ها',
      'Give someone a login limited to certain servers and rights. The owner always keeps full access.': 'به کسی دسترسی محدود به سرورها و اختیارات مشخص بدهید. مالک همیشه دسترسی کامل دارد.',


      /* ---- HTML-entity forms and status text ---- */
      'Sub-admins, their servers &amp; rights': 'ادمین‌های واسطه، سرورها و دسترسی‌ها',
      'Password &amp; two-factor': 'رمز عبور و ورود دو مرحله‌ای',
      'Thresholds, intervals, cache &amp; currency': 'آستانه‌ها، بازه‌ها، کش و واحد پول',
      'Configured · stopped': 'پیکربندی‌شده · متوقف',
      'unreachable': 'در دسترس نیست',
      'as': 'به‌عنوان',
      'but stopped.': 'ولی متوقف است.',
      'yet.': 'هنوز.',
      'Invalid backup file': 'فایل پشتیبان نامعتبر',


      /* ---- chips, palette commands, admin summaries ---- */
      '10 GB': '۱۰ گیگ',
      '50 GB': '۵۰ گیگ',
      '100 GB': '۱۰۰ گیگ',
      '30 days': '۳۰ روز',
      '90 days': '۹۰ روز',
      '1 year': '۱ سال',
      'Full access to everything': 'دسترسی کامل به همه‌چیز',
      'rights': 'دسترسی',
      'no rights': 'بدون دسترسی',


      /* ---- panel settings, keyed by knob so the server owns the spec ---- */
      'knob.notify_limit_percent.label': 'هشدار در چند درصد از حجم',
      'knob.notify_limit_percent.help': 'وقتی مصرف کاربر از این نسبت گذشت به ادمینِ کلید هشدار بده.',
      'knob.notify_expiry_days.label': 'هشدار چند روز قبل از انقضا',
      'knob.notify_expiry_days.help': '۰ هشدار انقضا را خاموش می‌کند.',
      'knob.expiry_check_interval.label': 'بازه زمان‌بند (ثانیه)',
      'knob.expiry_check_interval.help': 'هر چند وقت فعال‌سازی، ریست، هشدار و انقضا بررسی شود. کمتر یعنی بار بیشتر روی هر سرور Outline.',
      'knob.cycle_days.label': 'دوره صورتحساب (روز)',
      'knob.cycle_days.help': 'طول یک دوره سهمیه ماهانه.',
      'knob.metrics_ttl.label': 'کش متریک (ثانیه)',
      'knob.metrics_ttl.help': '۰ کش را کاملاً خاموش می‌کند — آن‌وقت هر بار تازه‌سازی داشبورد به endpoint آزمایشی هر سرور می‌خورد.',
      'knob.sub_update_hours.label': 'تازه‌سازی اشتراک (ساعت)',
      'knob.sub_update_hours.help': 'هر چند وقت به برنامه‌های VPN گفته شود اشتراک را دوباره بگیرند.',
      'knob.sub_cache_seconds.label': 'کش صفحه اشتراک (ثانیه)',
      'knob.sub_cache_seconds.help': 'لینک اشتراک عمومی است و هر بار به همه سرورها می‌رسد، پس یک کلاینت با حلقه تازه‌سازی تند بار را چند برابر می‌کند. ۰ کش را خاموش می‌کند.',
      'knob.sub_max_per_minute.label': 'دریافت اشتراک در دقیقه، برای هر آدرس',
      'knob.sub_max_per_minute.help': 'بیش از این با خطای ۴۲۹ رد می‌شود.',
      'knob.session_max_age.label': 'طول عمر نشست (ثانیه)',
      'knob.session_max_age.help': 'یک ورود چقدر معتبر می‌ماند.',
      'knob.login_max_fails.label': 'ورود ناموفق برای هر IP',
      'knob.login_max_fails.help': 'چند تلاش از یک آدرس در بازه زیر مجاز است.',
      'knob.login_window.label': 'بازه محدودیت ورود (ثانیه)',
      'knob.login_global_max_fails.label': 'کل ورودهای ناموفق',
      'knob.login_global_max_fails.help': 'سقف روی همه آدرس‌ها، تا چرخاندن IP از محدودیت هر IP عبور نکند.',
      'knob.health_alert_failures.label': 'هشدار بعد از این تعداد بررسی ناموفق',
      'knob.health_alert_failures.help': 'پشت‌سرهم، نه مجموع — یک بررسی ناموفق یک لحظه است و هشدار روی لحظه‌ها باعث می‌شود هشدارها نادیده گرفته شوند.',
      'knob.health_retention_days.label': 'نگهداری تاریخچه سلامت سرور (روز)',
      'knob.backup_interval_hours.label': 'پشتیبان خودکار هر (ساعت)',
      'knob.backup_interval_hours.help': 'عکس‌ها با VACUUM INTO گرفته می‌شوند و پنل را متوقف نمی‌کنند.',
      'knob.backup_keep.label': 'تعداد پشتیبان‌های نگه‌داشته',
      'knob.backup_keep.help': 'عکس‌های قدیمی‌تر وقتی این تعداد جدیدتر وجود داشت حذف می‌شوند.',
      'knob.audit_retention_days.label': 'نگهداری گزارش فعالیت (روز)',
      'knob.audit_retention_days.help': 'ورودی‌های قدیمی‌تر توسط زمان‌بند حذف می‌شوند. این گزارش فقط بزرگ می‌شود، پس همین جلویش را می‌گیرد.',
      'knob.currency.label': 'واحد پول',
      'knob.currency.help': 'کنار هر قیمت و موجودی نمایش داده می‌شود.',


      /* ---- package units ---- */
      'Unlimited data': 'حجم نامحدود',
      'GB': 'گیگ',
      'GB/month': 'گیگ در ماه',


      /* ---- table headers, status pills ---- */
      'Expiry / actions': 'انقضا / عملیات',
      'on use': 'از اولین اتصال',
      'd': ' روز',
      'Enable metrics sharing for a server (Settings) to see live bandwidth, online users and ISP locations.': 'برای دیدن پهنای باند زنده، کاربران آنلاین و موقعیت ISP، اشتراک‌گذاری متریک را برای یک سرور روشن کنید (تنظیمات).',


      /* ---- customer profile + rotation ---- */
      'New config': 'کانفیگ جدید',
      'New config issued': 'کانفیگ جدید صادر شد',
      'Working…': 'در حال انجام…',
      'Give “{name}” a new config? Their link stays the same, and the data they have already used is carried over.': 'برای «{name}» کانفیگ جدید صادر شود؟ لینکش عوض نمی‌شود و حجمی که مصرف کرده منتقل می‌شود.',
      'Customer profile site': 'سایت پروفایل مشتری',
      'Customer links': 'لینک‌های مشتری',
      'The short link you give customers': 'لینک کوتاهی که به مشتری می‌دهید',


      /* ---- customer profile settings ---- */
      'Point a subdomain here and customers get a short link like star.example.com/230-x7k2. It keeps working when you issue them a new config.': 'یک ساب‌دامین به اینجا وصل کنید تا مشتری یک لینک کوتاه مثل star.example.com/230-x7k2 بگیرد. وقتی کانفیگ جدید صادر کنید همان لینک کار می‌کند.',
      'Leave empty to serve everything on this one domain. Point the DNS at this server first.': 'خالی بگذارید تا همه‌چیز روی همین دامنه سرو شود. اول DNS را به این سرور وصل کنید.',


      /* ---- per-key customer link ---- */
      'Copy customer link': 'کپی لینک مشتری',
      'Customer link copied': 'لینک مشتری کپی شد',
      'This key has no customer link yet': 'این کلید هنوز لینک مشتری ندارد',
      'Link': 'لینک',

      /* ---- API error codes ---- */
      'err.credit.insufficient':
        'اعتبار کافی نیست: {package} قیمتش {price} {currency} است ولی موجودی شما {credit} {currency} است',
      'err.credit.must_buy_package': 'شما از لیست قیمت خرید می‌کنید — با انتخاب یک پکیج تمدید کنید',
      'err.credit.pick_package': 'یک پکیج انتخاب کنید',
      'err.package.unknown': 'پکیج نامعتبر',
      'err.server.unknown': 'سرور نامعتبر',
      'err.key.unknown': 'کلید نامعتبر',
      'err.admin.unknown': 'ادمین نامعتبر',
      'err.sub.unknown': 'اشتراک نامعتبر',
      'err.auth.forbidden': 'برای این کار دسترسی ندارید',
      'err.auth.owner_only': 'فقط مالک پنل',
      'err.auth.required': 'وارد نشده‌اید',
      'err.auth.expired': 'نشست منقضی شده',
      'err.auth.bad_credentials': 'نام کاربری یا رمز عبور اشتباه است',
      'err.auth.totp_required': 'کد دو مرحله‌ای لازم است',
      'err.auth.totp_invalid': 'کد دو مرحله‌ای نامعتبر',
      'err.auth.wrong_password': 'رمز عبور فعلی اشتباه است',
      'err.auth.rate_limited': 'تلاش‌های زیاد. چند دقیقه بعد دوباره امتحان کنید.',
      'err.sub.rate_limited': 'درخواست بیش از حد — کمی بعد دوباره تلاش کنید',
      'err.outline.unavailable': 'سرور Outline پاسخ نداد: {message}'
    }
  };

  var RTL = { fa: true };
  var lang = 'en';

  function pick() {
    try {
      var saved = localStorage.getItem('oc_lang');
      if (saved && DICT[saved]) return saved;
    } catch (e) { /* private mode: fall through to the browser's answer */ }
    var nav = (global.navigator && (navigator.language || navigator.userLanguage)) || 'en';
    var short = String(nav).toLowerCase().split('-')[0];
    return DICT[short] ? short : 'en';
  }

  function t(key, params) {
    var table = DICT[lang] || {};
    var out = table[key] != null ? table[key] : key;
    if (params) {
      out = out.replace(/\{(\w+)\}/g, function (m, name) {
        return params[name] != null ? params[name] : m;
      });
    }
    return out;
  }

  /* An API error, translated when we know the code and verbatim when we do not.
     `detail` is always English and always present, which is what makes an
     untranslated or brand-new error readable instead of blank. */
  function tErr(body) {
    if (!body) return t('Something went wrong');
    if (body.code) {
      var key = 'err.' + body.code;
      var msg = t(key, body.params);
      if (msg !== key) return msg;
    }
    return body.detail || t('Something went wrong');
  }

  function setLang(next) {
    if (!DICT[next]) return;
    lang = next;
    try { localStorage.setItem('oc_lang', next); } catch (e) { /* ignore */ }
    apply();
  }

  function apply() {
    var el = global.document && document.documentElement;
    if (!el) return;
    el.lang = lang;
    el.dir = RTL[lang] ? 'rtl' : 'ltr';
  }

  lang = pick();

  global.I18N = {
    t: t, tErr: tErr, setLang: setLang, apply: apply,
    get lang() { return lang; },
    isRTL: function () { return !!RTL[lang]; },
    languages: [{ id: 'en', label: 'English' }, { id: 'fa', label: 'فارسی' }]
  };
  global.t = t;
})(window);
