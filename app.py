"""
MBEYA SACCO WHATSAPP BOT
- Language detected from greeting, remembered for whole session
- Buttons return ONLY their own content, no redirect back to menu
- Goodbye messages are warm, SACCO-branded, no buttons returned
- Clean text only (no *, no markdown)
- Safe buttons (<=20 chars)
- AI thoughts/reasoning stripped from responses
- Deduplication: each WhatsApp message ID processed ONCE only
- Webhook always returns 200 instantly to stop WhatsApp retries
"""

import os
import re
import time
import logging
import json
import random
import asyncio
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from google import genai
import httpx
from concurrent.futures import ThreadPoolExecutor

# ─────────────────────────────
# ENV
# ─────────────────────────────
load_dotenv()

WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
VERIFY_TOKEN      = os.getenv("WHATSAPP_VERIFY_TOKEN", "fedecoach2024")

client   = genai.Client(api_key=GEMINI_API_KEY)
executor = ThreadPoolExecutor(max_workers=5)

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────
# DEDUPLICATION CACHE
#
# WhatsApp retries the same webhook event when your server is slow (>5s).
# We track every message ID we have already processed.
# If the same ID arrives again we return 200 immediately and do nothing.
# Cache entries expire after 10 minutes to avoid unbounded memory growth.
# ─────────────────────────────
PROCESSED_IDS: dict[str, float] = {}   # message_id -> timestamp
DEDUP_TTL = 600  # seconds (10 minutes)

def is_duplicate(msg_id: str) -> bool:
    now = time.time()
    expired = [k for k, ts in PROCESSED_IDS.items() if now - ts > DEDUP_TTL]
    for k in expired:
        del PROCESSED_IDS[k]
    if msg_id in PROCESSED_IDS:
        logging.info(f"DUPLICATE msg_id={msg_id} — ignored")
        return True
    PROCESSED_IDS[msg_id] = now
    return False

# ─────────────────────────────
# MEMORY
# ─────────────────────────────
USER_STATE = {}
USER_LANG  = {}

def set_state(user, state): USER_STATE[user] = state
def get_state(user):        return USER_STATE.get(user, "start")
def set_lang(user, lang):   USER_LANG[user] = lang
def get_lang(user):         return USER_LANG.get(user, "en")

# ─────────────────────────────
# LANGUAGE DETECTION
# ─────────────────────────────
SWAHILI_GREETINGS = [
    "habari", "jambo", "mambo", "karibu", "salamu", "hujambo",
    "niaje", "sasa", "nzuri", "shikamoo", "marahaba", "sijambo",
    "hamjambo", "mzuri", "poa", "safi", "habari yako",
    "habari za asubuhi", "habari za mchana", "habari za jioni",
]
ENGLISH_GREETINGS = [
    "hello", "hi", "hey", "good morning", "good afternoon",
    "good evening", "greetings", "howdy", "what's up", "sup",
    "good day", "morning", "evening", "afternoon", "hiya", "yo",
]
SWAHILI_GOODBYES = [
    "asante", "asante sana", "kwa heri", "kwaheri", "baadaye",
    "tutaonana", "nakushukuru", "nashukuru", "nimekwisha",
    "nimemaliza", "ok asante", "sawa asante", "ahsante",
    "sawa", "nimepata", "nimeelewa", "ok",
]
ENGLISH_GOODBYES = [
    "thanks", "thank you", "thank you so much", "bye", "goodbye",
    "see you", "see ya", "later", "cheers", "ok thanks",
    "okay thanks", "noted thanks", "great thanks", "perfect thanks",
    "done", "ok bye", "that's all", "thats all", "no more",
    "got it", "understood", "noted", "okay", "ok", "cool",
]

def detect_language(text: str):
    lower = text.lower().strip()
    for w in SWAHILI_GREETINGS:
        if w in lower:
            return "sw"
    for w in ENGLISH_GREETINGS:
        if w in lower:
            return "en"
    return None

def is_goodbye(text: str) -> bool:
    lower = text.lower().strip()
    for w in SWAHILI_GOODBYES + ENGLISH_GOODBYES:
        if lower in (w, w + "!", w + "."):
            return True
    return False

# ─────────────────────────────
# SACCO DEFINITION DETECTION
# ─────────────────────────────
# FIX: Variable names were inconsistent (SACCOS_ prefix vs SACCO_ prefix).
# Standardised to SACCO_KEYWORDS_SW and SACCO_KEYWORDS_EN throughout.
SACCO_KEYWORDS_SW = [
    "sacco", "nini sacco", "sacco ni nini", "je sacco", "SACCO",
    "savings and credit", "akiba na mikopo", "kooperetiva", "cooperative",
    "cumulative savings", "habari kuhusu sacco", "somo la sacco",
    "SACCOS ni nini", "SACCOS",
]
SACCO_KEYWORDS_EN = [
    "what is sacco", "what is saccos", "sacco definition", "saccos definition",
    "tell me about sacco", "tell me about saccos", "SACCO", "SACCOS",
    "savings and credit", "explain sacco", "sacco meaning", "saccos meaning",
    "cooperative society", "cooperative",
]

def is_sacco_definition_question(text: str, lang: str) -> bool:
    """Check if user is asking about what SACCO/SACCOS is."""
    lower = text.lower().strip()
    # FIX: now correctly references SACCO_KEYWORDS_SW / SACCO_KEYWORDS_EN
    keywords = SACCO_KEYWORDS_SW if lang == "sw" else SACCO_KEYWORDS_EN
    for keyword in keywords:
        if keyword.lower() in lower:
            return True
    return False

# ─────────────────────────────
# GEMINI HELPER
# Retries on 429; aborts on expired/invalid key or other permanent errors.
# ─────────────────────────────
async def _gemini_call(contents: str, system: str = None) -> str:
    max_retries = 3
    delay       = 5

    for attempt in range(max_retries):
        try:
            loop = asyncio.get_event_loop()

            def _call():
                kwargs = {"model": "gemini-2.5-flash", "contents": contents}
                if system:
                    kwargs["config"] = {"system_instruction": system}
                return client.models.generate_content(**kwargs).text

            return await loop.run_in_executor(executor, _call)

        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                if attempt < max_retries - 1:
                    logging.warning(f"Gemini 429 attempt {attempt+1}/{max_retries}, retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logging.error("Gemini 429 — all retries exhausted.")
                    return None
            else:
                logging.error(f"Gemini error [{type(e).__name__}]: {e}")
                return None

    return None


async def detect_language_ai(text: str) -> str:
    result = await _gemini_call(
        contents=(
            "Is this message in Swahili or English? "
            "Reply with ONLY one word: 'sw' or 'en'. "
            f"Message: {text}"
        )
    )
    if result:
        return "sw" if "sw" in result.strip().lower() else "en"
    return "en"

# ─────────────────────────────
# CLEAN TEXT + STRIP THOUGHTS
# ─────────────────────────────
def clean_text(text, max_len=1000):
    if not text:
        return ""
    for ch in ["*", "_", "`", "#"]:
        text = text.replace(ch, "")
    return text.strip()[:max_len]

def strip_thoughts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'THOUGHTS?:.*?(?=\n[A-Z]|\Z)', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(
        r'^(My |I need to|Let me|Okay,|Alright,|Sure,|Of course,|The user|Looking at).*\n?',
        '', text, flags=re.MULTILINE | re.IGNORECASE
    )
    return text.strip()

# ─────────────────────────────
# AI FALLBACK
# ─────────────────────────────
SYSTEM_PROMPT_SW = (
    "Wewe ni msaidizi wa MBEYA SACCO kwenye WhatsApp. "
    "Jibu kwa Kiswahili tu. Majibu mafupi na wazi. "
    "Hakuna markdown, hakuna *, hakuna alama za ziada. "
    "Hakuna mawazo, hakuna maelezo ya ndani, hakuna THOUGHTS, hakuna sababu. "
    "Toa JIBU TU moja kwa moja bila utangulizi wowote. "
    "Huduma: Akiba (TZS 10,000), Mikopo (TZS 50,000-5,000,000), Uwekezaji. "
    "Kama swali halikuhusiani na SACCO jibu: Samahani, ninatoa taarifa za MBEYA SACCO tu."
)
SYSTEM_PROMPT_EN = (
    "You are a MBEYA SACCO WhatsApp assistant. "
    "Reply in English only. Keep replies short and clear. "
    "No markdown, no *, no symbols. "
    "No thoughts, no reasoning, no THOUGHTS blocks, no preamble of any kind. "
    "Output ONLY the final answer directly, nothing else. "
    "Services: Savings (from TZS 10,000), Loans (TZS 50,000-5,000,000), Investments. "
    "If unrelated to SACCO reply: Sorry, I only provide information about MBEYA SACCO services."
)

SYSTEM_PROMPT_SACCO_EDU_SW = (
    "Wewe ni mtaalam wa SACCO kwenye WhatsApp. "
    "Jibu kwa Kiswahili tu. Eleza SACCO kwa namna kamili na muhimu. "
    "Andika kuhusu: (1) SACCO ni nini, (2) historia, (3) faida, (4) huduma, (5) jinsi ya kujiunga. "
    "Hakuna markdown, hakuna *, hakuna alama za ziada. "
    "Hakuna mawazo, hakuna maelezo ya ndani. Toa JIBU KAMILI na muhimu TU. "
    "FOCUS: SACCO only - Savings and Credit Cooperative Organisation."
)
SYSTEM_PROMPT_SACCO_EDU_EN = (
    "You are a SACCO expert on WhatsApp. "
    "Reply in English only. Explain SACCO comprehensively and thoroughly. "
    "Cover: (1) What is SACCO, (2) History, (3) Benefits, (4) Services, (5) How to join. "
    "No markdown, no *, no symbols. "
    "No thoughts, no reasoning. Provide COMPLETE and THOROUGH information ONLY. "
    "FOCUS: SACCO only - Savings and Credit Cooperative Organisation. "
    "Answer ALL questions about SACCO in detail and with authority."
)

async def ask_ai(user: str, msg: str, is_sacco_edu: bool = False) -> str:
    lang = get_lang(user)
    if is_sacco_edu:
        system = SYSTEM_PROMPT_SACCO_EDU_SW if lang == "sw" else SYSTEM_PROMPT_SACCO_EDU_EN
    else:
        system = SYSTEM_PROMPT_SW if lang == "sw" else SYSTEM_PROMPT_EN
    result = await _gemini_call(contents=msg, system=system)
    if result:
        return clean_text(strip_thoughts(result), max_len=4096)
    return (
        "Samahani, huduma ya maswali haipo sasa hivi. Tafadhali wasiliana nasi moja kwa moja."
        if lang == "sw" else
        "Sorry, the Q&A service is temporarily unavailable. Please contact us directly."
    )

# ─────────────────────────────
# STATIC CONTENT
# ─────────────────────────────
CONTENT = {
    "sw": {
        "welcome": (
            "Karibu MBEYA SACCO!\n\n"
            "Tunafurahi kukuona hapa.\n"
            "Ungependa kuanza wapi?"
        ),
        "services": (
            "Huduma zetu za MBEYA SACCO:\n\n"
            "- Akiba: Hifadhi pesa yako salama na upate riba\n"
            "- Mikopo: Pata mkopo wa haraka bila usumbufu\n"
            "- Uwekezaji: Ongeza thamani ya pesa yako\n\n"
            "Ungependa kujua zaidi kuhusu ipi?"
        ),
        "savings": (
            "AKIBA - MBEYA SACCO\n\n"
            "Kiwango cha kuanza: TZS 10,000 tu\n"
            "Riba: 8% kwa mwaka\n"
            "Aina: Akiba ya kawaida na ya muda maalum\n\n"
            "Jinsi ya kuanza:\n"
            "1. Tembelea ofisi yetu Barabara ya Jacaranda, Mbeya\n"
            "2. Lete kitambulisho chako (NIDA au passport)\n"
            "3. Lipa ada ya kujiunga TZS 5,000 mara moja tu\n"
            "4. Weka akiba yako ya kwanza\n\n"
            "Faida za akiba yetu:\n"
            "- Usalama kamili wa pesa yako\n"
            "- Riba inayokua kila mwaka\n"
            "- Upatikanaji wa mkopo baada ya miezi 3\n\n"
            "Kidokezo: Weka akiba kila mwezi, hata kidogo, "
            "na utaona mabadiliko makubwa maishani mwako!"
        ),
        "loans": (
            "MIKOPO - MBEYA SACCO\n\n"
            "Kiasi: TZS 50,000 hadi 5,000,000\n"
            "Riba: 12% kwa mwaka (1% kwa mwezi tu)\n"
            "Muda wa kulipa: Miezi 1 hadi 36\n\n"
            "Masharti ya kupata mkopo:\n"
            "1. Uwe mwanachama kwa angalau miezi 3\n"
            "2. Akiba yako iwe angalau 1/3 ya mkopo unaotaka\n"
            "3. Dhamana: mwanachama mwingine wa SACCO au mali\n"
            "4. Kuwa na historia nzuri ya malipo\n\n"
            "Aina za mikopo:\n"
            "- Mkopo wa dharura (wiki 1)\n"
            "- Mkopo wa biashara\n"
            "- Mkopo wa elimu\n"
            "- Mkopo wa nyumba\n\n"
            "Kidokezo: Panga mkopo unaoweza kulipa kwa utulivu. "
            "SACCO yetu ipo kukusaidia, si kukulemea!"
        ),
        "invest": (
            "UWEKEZAJI - MBEYA SACCO\n\n"
            "Fanya pesa yako ifanye kazi kwa ajili yako!\n\n"
            "UWEKEZAJI WA MUDA MFUPI (Miezi 3 - 12):\n"
            "- Riba: 10% kwa mwaka\n"
            "- Kiwango cha chini: TZS 50,000\n"
            "- Unaweza kufuta baada ya muda kukwisha\n\n"
            "UWEKEZAJI WA MUDA MREFU (Miaka 1 - 5):\n"
            "- Riba: 15% kwa mwaka\n"
            "- Kiwango cha chini: TZS 100,000\n"
            "- Inafaa kwa malengo ya mustakabali\n\n"
            "Mfano wa faida:\n"
            "TZS 500,000 kwa miaka 3 (15%) = TZS 725,000 na zaidi\n\n"
            "Kidokezo: Anza leo hata kidogo. "
            "Uwekezaji wa mapema ndio siri ya utajiri wa kweli!"
        ),
        "faqs": (
            "MASWALI YANAYOULIZWA MARA KWA MARA\n\n"
            "S: Ninawezaje kujiunga na MBEYA SACCO?\n"
            "J: Tembelea ofisi yetu, lete kitambulisho, lipa ada ya TZS 5,000\n\n"
            "S: Je, pesa yangu iko salama?\n"
            "J: Kabisa. SACCO yetu inasimamiwa na Benki Kuu ya Tanzania\n\n"
            "S: Ninaweza kutoa pesa yangu lini?\n"
            "J: Wakati wowote wa saa za kazi: Jumatatu-Ijumaa, 8am-5pm\n\n"
            "S: Kuna ada za kila mwezi?\n"
            "J: Hapana. Hulipa ada yoyote ya ziada baada ya kujiunga\n\n"
            "S: Mkopo wangu utachukua muda gani kupitishwa?\n"
            "J: Mikopo ya kawaida inachukua siku 3-7 za kazi\n\n"
            "S: Naweza kuwa mwanachama kama sina akaunti ya benki?\n"
            "J: Ndiyo. SACCO yetu inafanya kazi bila kuhitaji akaunti ya benki"
        ),
        "tips": (
            "VIDOKEZO VYA FEDHA KUTOKA MBEYA SACCO\n\n"
            "1. Weka akiba angalau 20% ya mshahara wako kila mwezi bila kukosa\n\n"
            "2. Fanya bajeti kila mwezi. Andika mapato yako yote na gharama zako zote\n\n"
            "3. Epuka madeni ya matumizi ya starehe. Kopa tu kwa mahitaji ya kweli\n\n"
            "4. Wekeza mapema. Shilingi 1,000 unayoweka leo itakuwa zaidi ya 2,000 baadaye\n\n"
            "5. Jenga akiba ya dharura sawa na matumizi ya miezi 3 "
            "kabla ya uwekezaji wowote\n\n"
            "6. Usitegemee mkopo mmoja kulipa mwingine. "
            "Panga vizuri kabla ya kukopa\n\n"
            "7. Jifunze kuhusu fedha kila wakati. "
            "Maarifa ni nguvu ya kweli ya utajiri\n\n"
            "MBEYA SACCO ipo hapa kukusaidia kufikia uhuru wako wa kifedha!"
        ),
        "contact": (
            "WASILIANA NA MBEYA SACCO\n\n"
            "Ofisi: Barabara ya Jacaranda, Mbeya Mjini\n"
            "Simu: +255 25 250 XXXX\n"
            "WhatsApp: +255 25 250 XXXX\n"
            "Email: info@mbeyasacco.co.tz\n\n"
            "Saa za kazi:\n"
            "Jumatatu-Ijumaa: 8:00am - 5:00pm\n"
            "Jumamosi: 8:00am - 12:00pm\n\n"
            "Andika ujumbe wako hapa chini. "
            "Timu yetu itakujibu haraka iwezekanavyo."
        ),
        "write_msg": (
            "Tafadhali andika ujumbe wako sasa. "
            "Tutaupeleka moja kwa moja kwa timu yetu:"
        ),
        "msg_received": (
            "Ujumbe wako umepokelewa salama.\n"
            "Timu yetu itakujibu ndani ya masaa 24.\n"
            "Asante kwa kuwasiliana na MBEYA SACCO!"
        ),
        "help_intro": (
            "Tunafurahi kukusaidia!\n"
            "Una maswali au unataka vidokezo vya fedha?"
        ),
        "goodbye": [
            "Asante kwa kututembelea leo! Kumbuka, akiba ya leo ni uhuru wa kesho. "
            "Karibu tena MBEYA SACCO wakati wowote. Tutaonana!",

            "Nakushukuru sana kwa muda wako. MBEYA SACCO ipo hapa "
            "kukusaidia kila hatua ya safari yako ya kifedha. Kwa heri na usisahau kuweka akiba!",

            "Tutaonana! Kila siku ni fursa mpya ya kuweka akiba na kukua kifedha. "
            "Tunakupenda na tunakusubiri tena. MBEYA SACCO - Nguvu yako ya Fedha!",

            "Asante sana! Safari yako ya kifedha inaanza na hatua moja, "
            "na wewe umeshafanya hivyo. MBEYA SACCO inakuamini na inakusaidia. Kwa heri!",

            "Kwa heri na safari njema! Usisahau: akiba ndogo ya kila siku "
            "inakuwa utajiri mkubwa kesho. MBEYA SACCO itakuwa hapa ukihitaji msaada wowote!",
        ],
    },
    "en": {
        "welcome": (
            "Welcome to MBEYA SACCO!\n\n"
            "We are delighted to have you here.\n"
            "Where would you like to start?"
        ),
        "services": (
            "MBEYA SACCO Services:\n\n"
            "- Savings: Keep your money safe and earn interest\n"
            "- Loans: Get fast, affordable loans\n"
            "- Investments: Grow the value of your money\n\n"
            "Which one would you like to know more about?"
        ),
        "savings": (
            "SAVINGS - MBEYA SACCO\n\n"
            "Minimum deposit: TZS 10,000 only\n"
            "Interest rate: 8% per year\n"
            "Types: Regular savings and fixed deposit\n\n"
            "How to get started:\n"
            "1. Visit our office on Jacaranda Road, Mbeya\n"
            "2. Bring your national ID (NIDA or passport)\n"
            "3. Pay a one-time joining fee of TZS 5,000\n"
            "4. Make your first deposit\n\n"
            "Benefits of saving with us:\n"
            "- Your money is fully secure and regulated\n"
            "- Annual interest that grows year by year\n"
            "- Access to loans after just 3 months of saving\n\n"
            "Tip: Save every month, even a small amount. "
            "Consistency is the real secret to financial freedom!"
        ),
        "loans": (
            "LOANS - MBEYA SACCO\n\n"
            "Amount: TZS 50,000 to 5,000,000\n"
            "Interest: 12% per year (only 1% per month)\n"
            "Repayment period: 1 to 36 months\n\n"
            "Requirements:\n"
            "1. Be a member for at least 3 months\n"
            "2. Your savings must be at least 1/3 of the loan amount\n"
            "3. Guarantor: another SACCO member or an asset\n"
            "4. Good repayment history\n\n"
            "Loan types available:\n"
            "- Emergency loan (processed in 1 week)\n"
            "- Business loan\n"
            "- Education loan\n"
            "- Housing loan\n\n"
            "Tip: Only borrow what you truly need and can comfortably repay. "
            "We are here to help you grow, not to burden you!"
        ),
        "invest": (
            "INVESTMENTS - MBEYA SACCO\n\n"
            "Put your money to work for you!\n\n"
            "SHORT TERM (3 - 12 months):\n"
            "- Interest: 10% per year\n"
            "- Minimum: TZS 50,000\n"
            "- Withdraw your funds when the term ends\n\n"
            "LONG TERM (1 - 5 years):\n"
            "- Interest: 15% per year\n"
            "- Minimum: TZS 100,000\n"
            "- Ideal for future goals and retirement planning\n\n"
            "Example:\n"
            "TZS 500,000 invested for 3 years at 15% = TZS 725,000 and above\n\n"
            "Tip: Start investing today, even a small amount. "
            "Time is the most powerful ingredient in building wealth!"
        ),
        "faqs": (
            "FREQUENTLY ASKED QUESTIONS\n\n"
            "Q: How do I join MBEYA SACCO?\n"
            "A: Visit our office, bring your ID, and pay a TZS 5,000 joining fee\n\n"
            "Q: Is my money safe?\n"
            "A: Absolutely. We are fully regulated by the Bank of Tanzania\n\n"
            "Q: When can I withdraw my savings?\n"
            "A: Any time during working hours: Mon-Fri, 8am-5pm\n\n"
            "Q: Are there monthly charges?\n"
            "A: No. There are no extra charges after joining\n\n"
            "Q: How long does loan approval take?\n"
            "A: Standard loans take 3-7 working days\n\n"
            "Q: Can I join without a bank account?\n"
            "A: Yes. SACCO membership does not require a bank account"
        ),
        "tips": (
            "FINANCIAL TIPS FROM MBEYA SACCO\n\n"
            "1. Save at least 20% of your income every single month without exception\n\n"
            "2. Create a monthly budget. Write down every shilling that comes in "
            "and every shilling that goes out\n\n"
            "3. Avoid lifestyle debts. Borrow only for genuine needs that add real value\n\n"
            "4. Start investing early. TZS 1,000 saved today will be worth "
            "more than TZS 2,000 in the future\n\n"
            "5. Build an emergency fund equal to 3 months of expenses "
            "before any other investment\n\n"
            "6. Never use one loan to repay another. "
            "Plan carefully before you borrow\n\n"
            "7. Keep learning about money. "
            "Financial knowledge is the real foundation of lasting wealth\n\n"
            "MBEYA SACCO is here to walk with you on your journey to financial freedom!"
        ),
        "contact": (
            "CONTACT MBEYA SACCO\n\n"
            "Office: Jacaranda Road, Mbeya Town\n"
            "Phone: +255 25 250 XXXX\n"
            "WhatsApp: +255 25 250 XXXX\n"
            "Email: info@mbeyasacco.co.tz\n\n"
            "Working hours:\n"
            "Monday-Friday: 8:00am - 5:00pm\n"
            "Saturday: 8:00am - 12:00pm\n\n"
            "Type your message below and our team will get back to you as soon as possible."
        ),
        "write_msg": (
            "Please type your message now. "
            "We will forward it directly to our team:"
        ),
        "msg_received": (
            "Your message has been received safely.\n"
            "Our team will reply within 24 hours.\n"
            "Thank you for reaching out to MBEYA SACCO!"
        ),
        "help_intro": (
            "We are happy to help!\n"
            "Do you have a question or would you like some financial tips?"
        ),
        "goodbye": [
            "Thank you for visiting MBEYA SACCO today! Remember, the savings you "
            "make today are the freedom you enjoy tomorrow. See you soon, we are always here!",

            "Thanks so much for your time! MBEYA SACCO is always here whenever you "
            "need us. Keep saving, keep growing, and keep building your future. Goodbye!",

            "See you next time! Every day is a new opportunity to save and build "
            "the life you deserve. MBEYA SACCO will be right here when you return. Take care!",

            "Thank you for trusting MBEYA SACCO! Your financial journey has already "
            "begun and we are proud to be part of it. Come back any time. Goodbye!",

            "Goodbye and stay well! Small savings every day add up to big wealth "
            "tomorrow. MBEYA SACCO looks forward to serving you again very soon!",
        ],
    },
}

BUTTONS = {
    "sw": {
        "main":     [("services", "Huduma"), ("help", "Msaada"), ("contact", "Wasiliana")],
        "services": [("savings", "Akiba"), ("loans", "Mikopo"), ("invest", "Uwekezaji")],
        "help":     [("faqs", "Maswali ya Kawaida"), ("tips", "Vidokezo vya Fedha")],
        "contact":  [("send_msg", "Tuma Ujumbe")],
    },
    "en": {
        "main":     [("services", "Services"), ("help", "Help"), ("contact", "Contact")],
        "services": [("savings", "Savings"), ("loans", "Loans"), ("invest", "Investments")],
        "help":     [("faqs", "FAQs"), ("tips", "Financial Tips")],
        "contact":  [("send_msg", "Send Message")],
    },
}

def get_content(user, key):  return CONTENT[get_lang(user)].get(key, "")
def get_buttons(user, menu): return BUTTONS[get_lang(user)].get(menu, [])
def get_goodbye(user) -> str:
    msgs = CONTENT[get_lang(user)].get("goodbye", [])
    return random.choice(msgs) if msgs else "Goodbye!"

# ─────────────────────────────
# SEND HELPERS
# ─────────────────────────────
async def _send(payload: dict):
    url     = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    logging.info(f"PAYLOAD: {json.dumps(payload)}")
    async with httpx.AsyncClient() as c:
        res = await c.post(url, headers=headers, json=payload)
        if res.status_code != 200:
            logging.warning(f"WA API ERROR {res.status_code}: {res.text}")
        else:
            logging.info(f"STATUS: {res.status_code}")

async def send_text(to: str, text: str):
    await _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": clean_text(text)},
    })

async def send_buttons(to: str, text: str, buttons: list):
    text = clean_text(text, max_len=1024)
    safe = [
        {"type": "reply", "reply": {"id": b[0], "title": clean_text(b[1])[:20]}}
        for b in buttons if clean_text(b[1])
    ]
    if not safe:
        await send_text(to, text)
        return
    await _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": safe[:3]},
        },
    })

# ─────────────────────────────
# MENUS  (each sends EXACTLY ONE message)
# ─────────────────────────────
async def main_menu(user):
    await send_buttons(user, get_content(user, "welcome"), get_buttons(user, "main"))
    set_state(user, "main")

async def services_menu(user):
    await send_buttons(user, get_content(user, "services"), get_buttons(user, "services"))
    set_state(user, "services")

async def help_menu(user):
    await send_buttons(user, get_content(user, "help_intro"), get_buttons(user, "help"))
    set_state(user, "help")

async def contact_menu(user):
    await send_buttons(user, get_content(user, "contact"), get_buttons(user, "contact"))
    set_state(user, "contact")

# ─────────────────────────────
# PROCESS MESSAGE (runs in background after 200 is returned)
# ─────────────────────────────
async def process_message(sender: str, text: str, is_button: bool):
    state = get_state(sender)

    # ── Language detection ───────────────────────────────────────────
    if not is_button:
        if state == "start" or sender not in USER_LANG:
            detected = detect_language(text)
            if detected is None:
                detected = await detect_language_ai(text)
            set_lang(sender, detected)
            logging.info(f"Language set for {sender}: {detected}")
        else:
            detected = detect_language(text)
            if detected:
                set_lang(sender, detected)

    # ── Goodbye — ONE warm farewell, reset state ─────────────────────
    if not is_button and is_goodbye(text):
        await send_text(sender, get_goodbye(sender))
        set_state(sender, "start")
        return

    # ── START state: show main menu then stop ────────────────────────
    if state == "start":
        await main_menu(sender)
        return

    # ── Contact input: user is typing a free-text message ────────────
    if state == "contact_input" and not is_button:
        logging.info(f"ADMIN MSG from {sender}: {text}")
        await send_text(sender, get_content(sender, "msg_received"))
        set_state(sender, "main")
        return

    # ── SACCO Definition Question → AI with special system prompt ────
    if not is_button and is_sacco_definition_question(text, get_lang(sender)):
        reply = await ask_ai(sender, text, is_sacco_edu=True)
        await send_text(sender, reply)
        return

    # ── Button / menu routing (works from ANY state) ─────────────────
    if text == "services":
        await services_menu(sender)
    elif text == "help":
        await help_menu(sender)
    elif text == "contact":
        await contact_menu(sender)
    elif text == "savings":
        await send_text(sender, get_content(sender, "savings"))
    elif text == "loans":
        await send_text(sender, get_content(sender, "loans"))
    elif text == "invest":
        await send_text(sender, get_content(sender, "invest"))
    elif text == "faqs":
        await send_text(sender, get_content(sender, "faqs"))
    elif text == "tips":
        await send_text(sender, get_content(sender, "tips"))
    elif text == "send_msg":
        set_state(sender, "contact_input")
        await send_text(sender, get_content(sender, "write_msg"))
    else:
        # Free text question → AI fallback
        reply = await ask_ai(sender, text)
        await send_text(sender, reply)


# ─────────────────────────────
# WEBHOOK
# ─────────────────────────────
@app.post("/api/webhook")
async def webhook(req: Request):
    """
    Return 200 immediately, then process the message in the background.
    This prevents WhatsApp from timing out and resending the same event.
    The deduplication cache ensures that even if WhatsApp sends the event
    twice before our background task records the ID, we process it only once.
    """
    body = await req.json()

    try:
        # ── Safe extraction — ignore status/read-receipt events ──────
        entry = body.get("entry")
        if not entry:
            return {"status": "ok"}

        value = entry[0].get("changes", [{}])[0].get("value", {})

        if "messages" not in value:
            return {"status": "ok"}

        messages_list = value["messages"]
        if not messages_list:
            return {"status": "ok"}

        msg    = messages_list[0]
        sender = msg.get("from")
        msg_id = msg.get("id", "")

        if not sender:
            return {"status": "ok"}

        # ── DEDUPLICATION — ignore retries of already-processed events
        if msg_id and is_duplicate(msg_id):
            return {"status": "ok"}

        # ── Parse text ───────────────────────────────────────────────
        if msg.get("type") == "interactive":
            text      = msg["interactive"]["button_reply"]["id"]
            is_button = True
        else:
            text      = msg.get("text", {}).get("body", "").strip()
            is_button = False

        if not text:
            return {"status": "ok"}

        # ── Fire-and-forget: process in background, return 200 now ───
        asyncio.create_task(process_message(sender, text, is_button))

    except Exception as e:
        logging.error(f"Webhook parse error: {e}", exc_info=True)

    # Always return 200 immediately — this stops WhatsApp from retrying
    return {"status": "ok"}


# ─────────────────────────────
# VERIFICATION + HEALTH
# ─────────────────────────────
@app.get("/api/webhook")
async def verify_webhook(req: Request):
    p = req.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return int(p.get("hub.challenge"))
    return {"error": "Verification failed"}


@app.get("/")
def home():
    return {"status": "MBEYA SACCO BOT RUNNING"}


@app.get("/api/health")
async def health_check():
    result = await _gemini_call("Say 'OK' only")
    if result:
        return {"status": "ok", "gemini_api": "working", "response": result.strip()}
    return {"status": "degraded", "gemini_api": "unavailable - check API key or quota"}