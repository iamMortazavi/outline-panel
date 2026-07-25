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
