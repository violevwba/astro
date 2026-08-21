import os, re, json, sqlite3, logging
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

import httpx
import swisseph as swe
from timezonefinder import TimezoneFinder
from openai import AsyncOpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrovilki")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
DB_PATH = os.getenv("DB_PATH", "astrovilki.sqlite3")
REPORT_DIR = os.getenv("REPORT_DIR", "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

llm = AsyncOpenAI(api_key=OPENAI_API_KEY)
tf = TimezoneFinder()
DATE, TIME, CITY = range(3)

SIGNS = ["Овен","Телец","Близнецы","Рак","Лев","Дева","Весы","Скорпион","Стрелец","Козерог","Водолей","Рыбы"]
ELEMENTS = {"Овен":"Огонь","Лев":"Огонь","Стрелец":"Огонь","Телец":"Земля","Дева":"Земля","Козерог":"Земля","Близнецы":"Воздух","Весы":"Воздух","Водолей":"Воздух","Рак":"Вода","Скорпион":"Вода","Рыбы":"Вода"}
PLANETS = {
    "Солнце": swe.SUN, "Луна": swe.MOON, "Меркурий": swe.MERCURY,
    "Венера": swe.VENUS, "Марс": swe.MARS, "Юпитер": swe.JUPITER,
    "Сатурн": swe.SATURN, "Уран": swe.URANUS, "Нептун": swe.NEPTUNE,
    "Плутон": swe.PLUTO, "Северный узел": swe.MEAN_NODE,
}
ASPECTS = {"Соединение":0,"Секстиль":60,"Квадрат":90,"Тригон":120,"Оппозиция":180}
DISCLAIMER = ("\n\n⚠️ Астрология и «матрица судьбы» — эзотерическая интерпретация для "
              "саморефлексии, а не научный метод. Не используй её как единственное основание "
              "для медицинских, финансовых или юридических решений.")

SYSTEM = """Ты — AstroVilki, современный эзотерический консультант.
Пиши на русском языке, тепло, подробно и понятно. Используй только переданные расчётные данные.
Не выдумывай положения планет, дома, аспекты или даты. Астрологию и нумерологию представляй
как символическую эзотерическую систему, а не доказанную науку. Не ставь диагнозы и не давай
юридических/финансовых указаний. Прогнозы формулируй как возможные темы и периоды, а не как
неизбежные события.
"""

PROMPTS = {
"natal": """Сделай ПОЛНЫЙ разбор натальной карты. Обязательно разберись с Асцендентом и МС,
Солнцем, Луной, Меркурием, Венерой, Марсом, Юпитером, Сатурном, Ураном, Нептуном, Плутоном
и Северным узлом; для каждой планеты укажи знак, дом и смысл. Разбери дома 1–12, главные аспекты
с орбами, ретроградность, сильные стороны и напряжения. Отдельные разделы: характер, эмоции,
любовь и отношения, сексуальность без откровенных деталей, деньги, профессия, таланты,
коммуникация, семья, переезды/путешествия, точки роста и итог. Если какой-то вывод не следует
из данных, не придумывай его.""",
"matrix": """Сделай подробный разбор Матрицы судьбы по 22 арканам. Разъясни каждое полученное число,
центральную энергию, личное предназначение, социальное/родовое предназначение, отношения,
деньги, таланты, ресурсы и теневые проявления. Отмечай, что это эзотерическая нумерология.""",
"solar": """Сделай подробный разбор соляра на указанный год: момент соляра, Асцендент и МС соляра,
планеты по домам и знакам, главная тема года, отношения, деньги, карьера, обучение, дом,
путешествия, внутренние изменения и вероятные активные периоды. Не выдавай события за гарантию.""",
"transits": """Сделай разбор текущих транзитов относительно натальной карты. Покажи найденные транзитные
планеты и аспекты к натальным планетам с орбами. Особое внимание медленным планетам, Юпитеру,
Сатурну и узлам. Для каждого аспекта объясни символическую тему и мягкую практическую рекомендацию.""",
"full": """Сделай максимально большой персональный отчёт: натальная карта + Матрица судьбы + соляр
+ текущие транзиты. Ничего не пропускай. Отдельно дай: личность, эмоциональный мир, отношения,
деньги, профессия, таланты, семью, коммуникацию, сильные стороны, тени, точки роста, тему года,
транзитные акценты и итоговые рекомендации. В начале дай краткое резюме, затем глубокий разбор."""
}

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        tg_id INTEGER PRIMARY KEY, name TEXT, birth_date TEXT, birth_time TEXT, birth_city TEXT,
        lat REAL, lon REAL, timezone TEXT, tz_offset REAL, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, kind TEXT, content TEXT, created_at TEXT)""")
    con.commit(); return con

def save_user(tg_id, name, d, t, city, lat, lon, tzname, offset):
    con=db(); con.execute("""INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name,birth_date=excluded.birth_date,
    birth_time=excluded.birth_time,birth_city=excluded.birth_city,lat=excluded.lat,lon=excluded.lon,
    timezone=excluded.timezone,tz_offset=excluded.tz_offset,updated_at=excluded.updated_at""",
    (tg_id,name,d.strftime("%d.%m.%Y"),t.strftime("%H:%M"),city,lat,lon,tzname,offset,datetime.utcnow().isoformat()))
    con.commit(); con.close()

def get_user(tg_id):
    con=db(); r=con.execute("SELECT * FROM users WHERE tg_id=?",(tg_id,)).fetchone(); con.close()
    if not r: return None
    keys=["tg_id","name","birth_date","birth_time","birth_city","lat","lon","timezone","tz_offset","updated_at"]
    return dict(zip(keys,r))

def save_report(tg_id,kind,content):
    con=db(); con.execute("INSERT INTO reports(tg_id,kind,content,created_at) VALUES(?,?,?,?)",(tg_id,kind,content,datetime.utcnow().isoformat())); con.commit(); con.close()

def parse_date(s):
    try: return datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except: return None

def parse_time(s):
    try: return datetime.strptime(s.strip(), "%H:%M").time()
    except: return None

async def geocode(city):
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent":"AstroVilkiBot/2.0"}) as c:
        r=await c.get("https://nominatim.openstreetmap.org/search",params={"q":city,"format":"jsonv2","limit":1})
        r.raise_for_status(); data=r.json()
    if not data: raise ValueError("Не нашла город. Напиши город и страну, например: Helsinki, Finland")
    lat=float(data[0]["lat"]); lon=float(data[0]["lon"]); name=data[0].get("display_name",city)
    tzname=tf.timezone_at(lat=lat,lng=lon) or "UTC"
    return lat,lon,name,tzname

def local_to_utc(d,t,tzname):
    local=datetime.combine(d,t).replace(tzinfo=ZoneInfo(tzname))
    utc=local.astimezone(ZoneInfo("UTC"))
    offset=local.utcoffset().total_seconds()/3600
    return utc,offset

def julian(d,t,tzname):
    utc,_=local_to_utc(d,t,tzname)
    return swe.julday(utc.year,utc.month,utc.day,utc.hour+utc.minute/60+utc.second/3600)

def pos_text(x):
    x=x%360; sign=SIGNS[int(x//30)]; deg=x%30
    return f"{sign} {deg:.2f}°"

def planet_calc(j):
    out={}
    for name,body in PLANETS.items():
        xx,flags=swe.calc_ut(j,body)
        out[name]={"longitude":xx[0]%360,"latitude":xx[1],"speed":xx[3],"position":pos_text(xx[0]),"retrograde":xx[3]<0}
    return out

def house_num(x,cusps):
    x%=360
    for i in range(12):
        a=cusps[i]%360; b=cusps[(i+1)%12]%360
        if (a<=b and a<=x<b) or (a>b and (x>=a or x<b)): return i+1
    return 1

def aspect(a,b):
    diff=abs((a-b)%360); diff=min(diff,360-diff)
    for name,angle in ASPECTS.items():
        orb=8 if name=="Соединение" else 6
        if abs(diff-angle)<=orb: return name,round(abs(diff-angle),2)
    return None

def natal_chart(d,t,lat,lon,tzname):
    j=julian(d,t,tzname); planets=planet_calc(j); cusps,asc,mc=swe.houses_ex(j,lat,lon,b'P')
    for p in planets.values(): p["house"]=house_num(p["longitude"],cusps)
    names=list(planets); aspects=[]
    for i,a in enumerate(names):
        for b in names[i+1:]:
            z=aspect(planets[a]["longitude"],planets[b]["longitude"])
            if z: aspects.append({"a":a,"b":b,"aspect":z[0],"orb":z[1]})
    houses=[{"house":i+1,"cusp":pos_text(c)} for i,c in enumerate(cusps)]
    return {"system":"Tropical zodiac, Placidus houses","utc_julian_day":j,"ascendant":pos_text(asc),"mc":pos_text(mc),"houses":houses,"planets":planets,"aspects":aspects}

def matrix22(d):
    def reduce22(n):
        while n>22: n=sum(int(x) for x in str(n))
        return n or 22
    raw=[int(x) for x in d.strftime("%d%m%Y")]
    day=reduce22(d.day); month=reduce22(d.month); year=reduce22(sum(raw[4:])); total=reduce22(day+month+year); center=reduce22(day+month+year+total)
    return {"day":day,"month":month,"year":year,"total":total,"center":center,"method":"22-арканная нумерологическая редукция"}

def solar_chart(d,t,lat,lon,tzname):
    natal_j=julian(d,t,tzname); target=planet_calc(natal_j)["Солнце"]["longitude"]
    # Search around birthday in the next year in 10-minute increments.
    start=datetime(d.year+1,d.month,d.day,t.hour,t.minute)-timedelta(days=2)
    best=None
    for i in range(7*24*6):
        local=start+timedelta(minutes=10*i)
        j=swe.julday(local.year,local.month,local.day,local.hour+local.minute/60)
        sun=swe.calc_ut(j,swe.SUN)[0][0]%360
        diff=min(abs(sun-target),360-abs(sun-target))
        if best is None or diff<best[0]: best=(diff,j,local)
    j=best[1]; planets=planet_calc(j); cusps,asc,mc=swe.houses_ex(j,lat,lon,b'P')
    for p in planets.values(): p["house"]=house_num(p["longitude"],cusps)
    return {"return_local_datetime":best[2].isoformat(),"error_degrees":round(best[0],5),"ascendant":pos_text(asc),"mc":pos_text(mc),"planets":planets}

def current_transits(natal):
    now=datetime.now()
    j=swe.julday(now.year,now.month,now.day,now.hour+now.minute/60+now.second/3600)
    tr=planet_calc(j); aspects=[]
    for tn,tp in tr.items():
        for nn,np in natal["planets"].items():
            z=aspect(tp["longitude"],np["longitude"])
            if z: aspects.append({"transit":tn,"natal":nn,"aspect":z[0],"orb":z[1]})
    return {"calculated_at_local":now.isoformat(),"planets":tr,"aspects_to_natal":aspects}

def payload_json(x): return json.dumps(x,ensure_ascii=False,indent=2)

async def ai_report(kind,payload):
    prompt=PROMPTS[kind]+"\n\nВот ТОЧНЫЕ РАСЧЁТНЫЕ ДАННЫЕ:\n"+payload_json(payload)+"\n\nНе придумывай отсутствующие значения."
    r=await llm.chat.completions.create(model=MODEL,messages=[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}],temperature=.75)
    return r.choices[0].message.content+DISCLAIMER

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌌 Натальная карта",callback_data="natal"),InlineKeyboardButton("✨ Полный разбор",callback_data="full")],
        [InlineKeyboardButton("☀️ Соляр",callback_data="solar"),InlineKeyboardButton("🔢 Матрица судьбы",callback_data="matrix")],
        [InlineKeyboardButton("🪐 Транзиты",callback_data="transits"),InlineKeyboardButton("👤 Мои данные",callback_data="profile")],
        [InlineKeyboardButton("♻️ Изменить данные",callback_data="reset")]
    ])

async def start(update,context):
    context.user_data.clear(); await update.message.reply_text("🌙 Добро пожаловать в AstroVilki!\n\nДата рождения в формате ДД.ММ.ГГГГ:"); return DATE
async def get_date(update,context):
    d=parse_date(update.message.text)
    if not d: await update.message.reply_text("Нужен формат ДД.ММ.ГГГГ, например 14.02.2001"); return DATE
    context.user_data["d"]=d; await update.message.reply_text("Время рождения, например 07:35:"); return TIME
async def get_time(update,context):
    t=parse_time(update.message.text)
    if not t: await update.message.reply_text("Нужен формат ЧЧ:ММ, например 07:35"); return TIME
    context.user_data["t"]=t; await update.message.reply_text("Город рождения, например Helsinki, Finland:"); return CITY
async def get_city(update,context):
    try: lat,lon,display,tzname=await geocode(update.message.text)
    except Exception as e: await update.message.reply_text(f"Не удалось найти город: {e}"); return CITY
    d=context.user_data["d"]; t=context.user_data["t"]; _,offset=local_to_utc(d,t,tzname)
    save_user(update.effective_user.id,update.effective_user.first_name or "",d,t,update.message.text,lat,lon,tzname,offset)
    await update.message.reply_text(f"Готово 🌌\nМесто: {display}\nЧасовой пояс: {tzname} (UTC{offset:+.1f})\n\nТеперь выбери разбор:",reply_markup=menu())
    return ConversationHandler.END

async def send_long(msg,text):
    for i in range(0,len(text),3900): await msg.reply_text(text[i:i+3900])

async def run_kind(update,context,kind):
    q=update.callback_query; await q.answer(); u=get_user(update.effective_user.id)
    if not u: await q.message.reply_text("Сначала нажми /start."); return
    await q.message.chat.send_action(ChatAction.TYPING)
    d=parse_date(u["birth_date"]); t=parse_time(u["birth_time"])
    n=natal_chart(d,t,u["lat"],u["lon"],u["timezone"])
    if kind=="natal": payload={"birth":{"date":u["birth_date"],"time":u["birth_time"],"city":u["birth_city"],"timezone":u["timezone"]},"natal":n}
    elif kind=="matrix": payload={"birth_date":u["birth_date"],"matrix":matrix22(d)}
    elif kind=="solar": payload={"birth":u["birth_date"],"natal":n,"solar":solar_chart(d,t,u["lat"],u["lon"],u["timezone"])}
    elif kind=="transits": payload={"natal":n,"transits":current_transits(n)}
    elif kind=="full": payload={"birth":{"date":u["birth_date"],"time":u["birth_time"],"city":u["birth_city"],"timezone":u["timezone"]},"natal":n,"matrix":matrix22(d),"solar":solar_chart(d,t,u["lat"],u["lon"],u["timezone"]),"transits":current_transits(n)}
    else: return
    text=await ai_report(kind,payload); save_report(update.effective_user.id,kind,text); await send_long(q.message,text); await q.message.reply_text("Готово 🌙",reply_markup=menu())

async def profile(update,context):
    await update.callback_query.answer(); u=get_user(update.effective_user.id)
    if not u: await update.callback_query.message.reply_text("Данных пока нет. Нажми /start."); return
    await update.callback_query.message.reply_text(f"👤 Данные\n\nДата: {u['birth_date']}\nВремя: {u['birth_time']}\nГород: {u['birth_city']}\nЧасовой пояс: {u['timezone']}\nКоординаты: {u['lat']:.5f}, {u['lon']:.5f}",reply_markup=menu())

async def reset(update,context):
    await update.callback_query.answer(); context.user_data.clear(); await update.callback_query.message.reply_text("Хорошо. Напиши новую дату рождения: ДД.ММ.ГГГГ"); return DATE

async def callback(update,context):
    d=update.callback_query.data
    if d in ("natal","full","solar","matrix","transits"): return await run_kind(update,context,d)
    if d=="profile": return await profile(update,context)
    if d=="reset": return await reset(update,context)

async def cancel(update,context): await update.message.reply_text("Отменено. /start — начать заново."); return ConversationHandler.END

def main():
    db(); app=Application.builder().token(TOKEN).build()
    onboarding=ConversationHandler(entry_points=[CommandHandler("start",start)],states={DATE:[MessageHandler(filters.TEXT & ~filters.COMMAND,get_date)],TIME:[MessageHandler(filters.TEXT & ~filters.COMMAND,get_time)],CITY:[MessageHandler(filters.TEXT & ~filters.COMMAND,get_city)]},fallbacks=[CommandHandler("cancel",cancel)])
    app.add_handler(onboarding); app.add_handler(CallbackQueryHandler(callback)); app.add_handler(CommandHandler("help",lambda u,c:u.message.reply_text("/start — новая анкета. После анкеты доступны натал, полный разбор, соляр, матрица и транзиты.")))
    log.info("AstroVilki started without Geocult dependency")
    app.run_polling()

if __name__=="__main__": main()
