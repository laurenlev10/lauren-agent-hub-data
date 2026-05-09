# 🚀 Trading Dashboard — Setup Guide (10 דקות)

מדריך התקנה לדשבורד מסחר עצמאי ב־GitHub Pages.
החבילה הזו זהה למערכת המסחר של Lauren אבל **בלי הנתונים שלה** — דשבורד נקי, מוכן להתחבר לחשבונות שלך.

> **למי זה מתאים:** סוחרים ב־**BluSky** / **Apex** / **Lucid** שרוצים מערכת מעקב אישית עם:
> - יבוא CSV מ־Tradovate (אוטומטי, לפי פלטפורמה)
> - פאנל "כמה עוד עד משיכה הבאה?"
> - חוקי כל פרופ פירם מובנים (24KB JSON, 22 שלבים, 17 מקורות מאומתים)
> - אסטרטגיית buffer בטוחה (לא משיכות על ה־MLL)
> - היסטוריית phase transitions (כשעוברים eval → BluLive → Funded)
> - מקסימום משיכות עם ladder לכל חברה
> - סנכרון בין כל המכשירים שלך (לפטופ / טלפון / דסקטופ)

---

## 📋 צעדים — תהליך פעם-אחת בלבד

### צעד 1 — חשבון GitHub (אם אין)
- היכנס ל־https://github.com
- "Sign up" → אימייל + סיסמה + שם משתמש (יישאר לתמיד)
- אמת אימייל

### צעד 2 — צור Repo משלך מהtemplate
- לחץ **"Use this template"** ב־repo הזה
- שם הrepo: **`trading-dashboard`** (חייב להיות בדיוק זה — הקוד מצפה לזה)
- Public/Private: **Public** (חובה ל־GitHub Pages חינם)
- "Create repository"

### צעד 3 — הפעל GitHub Pages
- ב־repo החדש שלך → **Settings** → **Pages** (תפריט שמאלי)
- Source: **"Deploy from a branch"**
- Branch: **`main`** + Folder: **`/docs`**
- "Save"
- חכה ~30 שניות → הדף יהיה זמין ב:
  ```
  https://[שם-המשתמש שלך].github.io/trading-dashboard/trading-template/
  ```

### צעד 4 — צור Personal Access Token (PAT)
ה־token הזה הוא שמאפשר לדפדפן לעדכן את הנתונים שלך ב־GitHub.

1. https://github.com/settings/tokens/new?scopes=repo
2. Note: **"Trading Dashboard Sync"**
3. Expiration: **"No expiration"** (או 1 year — תצטרך לחדש)
4. Scopes: ✅ **`repo`** (הסעיף השני — Full control of private repositories)
5. "Generate token"
6. **העתק את ה־token שמתחיל ב־`ghp_`** — תוצג רק פעם אחת!

### צעד 5 — חבר את ה־token לדשבורד
1. פתח את הדשבורד שלך:
   `https://[שם-משתמש].github.io/trading-dashboard/trading-template/`
2. לחץ על **"☁ Cloud: off"** למעלה ימין
3. הדבק את ה־token (שמתחיל `ghp_`)
4. אם הצליח: הכפתור הופך ירוק → **"☁ Synced"**

### צעד 6 — הוסף את החשבונות שלך
**אופציה א — דרך ה־UI:**
- לחץ על "+ Add Account" בכל לשונית (BluSky/Apex/Lucid)

**אופציה ב — ערוך את `docs/trading-template/state/trading.json`:**
- ערוך ב־GitHub את הקובץ ישירות
- כל חשבון = `{ "ph": "eval1", "tw": 0, "adj": 0, "days": [], "history": [] }`
- ל־apexAccs ו־lucidAccs צריך גם `meta` עם `name`, `num`, `startBal`, `trailDD`, ועוד

### צעד 7 — העלה CSV ראשון
1. ב־Tradovate → Account Reports → Export Performance.csv
2. בדשבורד: **"📂 ייבוא CSV"**
3. בחר חשבון מה־dropdown
4. גרור את ה־CSV
5. בדוק את התצוגה המקדימה
6. **"💾 החל על החשבון"**

הסטטוס יהפוך ירוק → **"✅ נשמר בענן ✓"** → המודאל ייסגר אוטומטית

---

## 💰 מודלי עמלות (חשוב לדעת)

הדשבורד יבחר אוטומטית את המודל הנכון לפי החברה:

| חברה | מודל עמלה | ברירת מחדל |
|---|---|---|
| **BluSky** | $X לעסקה (flat) | $2.20/trade |
| **Apex Legacy** | $X לעסקה (flat) | $3.45/trade |
| **Lucid** | $X לחוזה (per-contract) | $1.00/contract |

אם ה־balance שלך לא מסתדר עם מה שהברוקר מציג, ניתן לעדכן את המודל ב־UI לפני הייבוא.

---

## 🛡️ אסטרטגיית Buffer — חשוב!

הדשבורד מכוון לאסטרטגיה הבאה ב־payout phase:
- **לא** למשוך ב־minimum technical (משאיר את ה־balance בדיוק על ה־MLL)
- **כן** למשוך ב־**MLL + 2× cap** (משאיר buffer של תקרה מלאה אחרי המשיכה)

דוגמה ל־Lucid 50K:
- Min technical: $52,100 → אחרי משיכה $2,000, balance = $50,100 = MLL בדיוק = blowout בהפסד אחד
- **Buffer target: $54,100** → אחרי משיכה $2,000, balance = $52,100 = $2K מעל MLL ✓

זו לא רק המלצה — זה כתוב כ־IRON RULE ב־`prop_firm_rules.json`.

---

## ❓ פתרון בעיות

**הדשבורד טוען אבל אומר "Cloud: off":**
- בדוק שה־token מתחיל ב־`ghp_`
- בדוק ש־scope `repo` סומן בעת היצירה
- נסה לפתוח שוב → ☁ Cloud → הדבק שוב

**ה־CSV אומר "החשבון לא נמצא":**
- ודא ש־`state/trading.json` כולל את החשבון לפני שמייבאים
- בדוק שה־dropdown שלך מציג את כל החשבונות שיש לך

**Balance לא מסתדר עם הברוקר:**
- בדוק את מודל העמלה (BluSky $2.20 / Apex $3.45 / Lucid $1.00)
- אם פער של ~$X לעסקה → עדכן את ה־"עמלה" field ב־CSV importer

**ה־payout panel לא מוצג ב־Lucid:**
- הצב את ה־`ph` ל־`funded` (לא `eval`) — הפאנל מוצג רק במצב funded
- בדוק שה־`meta` של החשבון נכון

---

## 🔐 פרטיות

- **כל הנתונים שלך פרטיים לחלוטין.** ה־repo הוא Public לצורך GitHub Pages, אבל אף אחד לא יכול לשנות אותו בלי ה־token שלך.
- ה־token נשמר רק ב־localStorage של הדפדפן שלך. אף שרת אחר לא רואה אותו.
- אם אתה חושש לפרטיות — הפוך את ה־repo לPrivate (אבל אז GitHub Pages עולה כסף — $4/חודש).

---

## 🆘 צריך עזרה?

- בעיה טכנית: פתח Issue ב־repo המקורי
- שאלה אסטרטגית: שאל את Lauren

נבנה ע"י Lauren ב־Cowork × Claude Agent SDK. Open source.
