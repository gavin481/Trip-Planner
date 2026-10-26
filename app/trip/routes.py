"""HTTP routes for the Japan & Bali page at /trip.

The ema app calls handle_get and handle_post. Any other path returns None
so the rest of Lost Miniatures keeps its own routes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from trip import auth
from trip.storage import (
    PAGE,
    booking_pdf as _booking_pdf,
    data_dir as _data_dir,
    on_railway as _on_railway,
    published_stamp,
    store_path as _store_path,
)

cookie_header = auth.cookie_header
reset_password = auth.reset_password
ResetError = auth.ResetError
unlocked = auth.unlocked
_password_accepted = auth._password_accepted
_too_many_tries = auth._too_many_tries
_word_matches = auth._word_matches

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_KINDS = {"flight", "stay", "ticket", "train", "disney", "onsen", "other"}
_CATEGORIES = {"flight", "accom", "train", "disney", "ticket", "onsen"}
# Rate is units of that currency for 1 AUD. The yen rate is the one printed on the
# Booking.com confirmation (AUD 387.80 = 41,769 JPY), the rest are round numbers.
RATES_PER_AUD = {
    "AUD": 1.0,
    "JPY": 107.7,
    "IDR": 10800.0,
    "SGD": 0.86,
    "USD": 0.66,
    "EUR": 0.58,
    "NZD": 1.1,
    "GBP": 0.5,
}
_CURRENCIES = set(RATES_PER_AUD)
_LOCK = threading.Lock()
_PHOTO_LOCK = threading.Lock()
_LAST_LOOKUP = 0.0
_UA = "EMATrip/1.0 (personal itinerary photos)"
_PHOTO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ADDRESS_RE = re.compile(r"\d{3}-\d{4}|\d+(?:-\d+)+")
_STOP_WORDS = {"the", "and", "for", "from", "with", "near", "into"}
_GENERIC_WORDS = {"tokyo", "japan", "hotel", "station", "airport", "terminal", "line", "park", "spa"}

def _booking_label(item_id: str, stored) -> str:
    if not _PHOTO_ID_RE.fullmatch(item_id) or not _booking_pdf(item_id).is_file():
        return ""
    return _clean_text(stored, 120) or "Booking PDF"


def _attachments(raw: dict) -> list[dict]:
    """Every PDF on a booking. The first is the card's own file."""
    rows = []
    seen = set()

    def add(file_id, label) -> None:
        file_id = str(file_id or "").strip()
        if not file_id or file_id in seen or len(rows) >= 6:
            return
        name = _booking_label(file_id, label)
        if not name:
            return
        seen.add(file_id)
        rows.append({"id": file_id, "fileName": name})

    add(raw.get("id"), raw.get("fileName"))
    extra = raw.get("files") if isinstance(raw.get("files"), list) else []
    for item in extra:
        if isinstance(item, dict):
            add(item.get("id"), item.get("fileName"))
    return rows


def _pdf_download_name(label: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]", "", _clean_text(label, 120)).strip(" .")
    if not text:
        text = "booking"
    if not text.lower().endswith(".pdf"):
        text += ".pdf"
    return text


# November 2026. Shinjuku is 2 nights from the 17 Nov arrival, so checkout is 19 Nov.
SEED: list[dict] = [
    {
        "id": "jq34-mel-cns",
        "kind": "flight",
        "title": "Melbourne to Cairns",
        "subtitle": "Jetstar JQ34 · stopover",
        "date": "2026-11-17",
        "time": "06:15",
        "endTime": "08:30",
        "endDate": "",
        "booking": "DHHEPP",
        "detail": "Both Jetstar legs · 2 adults",
        "cost": 379.78,
        "currency": "AUD",
        "notes": "Depart Melbourne 6:15am. Arrive Cairns 8:30am.",
        "link": "https://www.google.com/maps/search/?api=1&query=Melbourne+Airport",
        "linkLabel": "Airport",
    },
    {
        "id": "jq25-cns-nrt",
        "kind": "flight",
        "title": "Cairns to Tokyo Narita",
        "subtitle": "Jetstar JQ25",
        "date": "2026-11-17",
        "time": "11:45",
        "endTime": "18:10",
        "endDate": "",
        "booking": "DHHEPP",
        "detail": "Paid on the DHHEPP booking",
        "notes": "Depart Cairns 11:45am. Land Tokyo Narita Terminal 3 at 6:10pm.",
        "link": "https://www.google.com/maps/search/?api=1&query=Narita+Airport+Terminal+3",
        "linkLabel": "Airport",
    },
    {
        "id": "narita-shinjuku",
        "kind": "train",
        "title": "Narita Airport to Shinjuku",
        "subtitle": "Narita Express · Southeast Exit for the hotel",
        "date": "2026-11-17",
        "time": "19:20",
        "endTime": "20:40",
        "endDate": "",
        "booking": "",
        "detail": "¥3,330 each",
        "cost": 6660,
        "currency": "JPY",
        "notes": "One-way ordinary reserved seat. Times are the JR East timetable for this season. Follow the station board if a train has moved.",
        "link": "https://www.google.com/maps/dir/?api=1&origin=JR+Shinjuku+Station+Southeast+Exit&destination=Anshin+Oyado+Tokyo+Man+Shinjuku&travelmode=walking",
        "linkLabel": "Walk to the hotel",
        "steps": [
            "Land at Narita Terminal 3. That terminal has no train station.",
            "From arrivals, take the free shuttle on the ground floor to Terminal 2. The ride is about 3 minutes. The covered walk is 10–15 minutes.",
            "Go to B1 in Terminal 2: Narita Airport Terminal 2·3 Station. Follow the red N'EX signs.",
            "Buy 2 one-way ordinary reserved seats to Shinjuku at the JR ticket office or the English ticket machine. Cash or card. ¥3,330 per adult. Skip the round-trip pass.",
            "If you are through quickly: N'EX 48 leaves Terminal 2·3 at 7:20pm and arrives Shinjuku at 8:40pm, platform 5. Stay on the train at Tokyo.",
            "Safer train: N'EX 50 leaves Terminal 2·3 at 7:53pm. It splits at Tokyo. Sit only in the cars marked Shinjuku (新宿), train 2250M. Do not sit in the Ofuna (大船) cars. Shinjuku at 9:18pm, platform 5.",
            "Backup: N'EX 52 leaves Terminal 2·3 at 8:47pm, same split, Shinjuku cars only. Arrives Shinjuku at 10:09pm, platform 6.",
            "At Shinjuku, follow Southeast Exit (東南口). The hotel is a 90-second walk: Shinkin Building, 2-10 Shinjuku 4-chome. McDonald's L-Breath, across from Busta Shinjuku, is the landmark.",
        ],
    },
    {
        "id": "shinjuku-anshin",
        "kind": "stay",
        "title": "Anshin Oyado Tokyo Man Shinjuku",
        "subtitle": "Booking.com · Gavin's profile",
        "date": "2026-11-17",
        "time": "",
        "endTime": "",
        "endDate": "2026-11-19",
        "booking": "6132.066.182",
        "detail": "Pin 3900",
        "cost": 387.80,
        "currency": "AUD",
        "phone": "+81 3-6856-3222",
        "notes": "4-2-10 Shinjuku, Shinjuku-ku, Tokyo 160-0022.",
        "link": "https://www.google.com/maps/search/?api=1&query=4-2-10+Shinjuku+Shinjuku-ku+Tokyo+160-0022",
        "linkLabel": "Address",
        "site": "https://en.anshin-oyado.jp/shop/shinjuku/",
        "siteLabel": "Hotel",
    },
    {
        "id": "shinjuku-disneyland",
        "kind": "train",
        "title": "Shinjuku to Disney Tokyo",
        "subtitle": "Chuo Line Rapid, then Keiyo Line to Maihama",
        "date": "2026-11-19",
        "time": "08:00",
        "endTime": "09:00",
        "endDate": "",
        "booking": "",
        "detail": "About ¥440 each",
        "cost": 880,
        "currency": "JPY",
        "notes": "No reserved seat. One Suica tap covers both trains. Park tickets are already booked.",
        "link": "https://www.google.com/maps/dir/?api=1&origin=JR+Shinjuku+Station&destination=Tokyo+Disneyland&travelmode=transit",
        "linkLabel": "Train map",
        "steps": [
            "Park entry is already booked for 2 adults. Use the QR in the confirmation email or the Tokyo Disney Resort app. The gate scans it. Do not buy another park ticket.",
            "The train is a Suica tap, about ¥440 each. Buy or charge a Suica at a JR Shinjuku ticket machine before you leave. There is no seat to reserve.",
            "Leave the hotel by 7:40am. At JR Shinjuku take the Chuo Line Rapid (中央線快速) toward Tokyo. About 15 minutes. Stay inside the gates at Tokyo Station.",
            "Follow the Keiyo Line signs (京葉線). The platform is underground, about a 10-minute walk. Board a train that stops at Maihama (舞浜). About 15 minutes.",
            "At Maihama use the South Exit and turn right. Tokyo Disneyland is a 5–8 minute walk. The monorail is the left turn, and you do not need it today.",
            "Be at the gate by 9:00am. Check the Disney app the day before, because the opening time can change.",
            "After the park, walk from Maihama to the wooden house. Check in is from 4:00pm. You are not going back to Shinjuku tonight.",
        ],
    },
    {
        "id": "disney-tokyo",
        "kind": "disney",
        "title": "Disney Tokyo",
        "subtitle": "",
        "date": "2026-11-19",
        "time": "",
        "endTime": "",
        "endDate": "",
        "booking": "A00626628114",
        "detail": "2 adults",
        "notes": "Show the QR from the confirmation email or the Tokyo Disney Resort app.",
        "link": "https://www.google.com/maps/search/?api=1&query=Tokyo+Disneyland",
        "linkLabel": "Park",
        "steps": [
            "Jungle River Cruise",
            "Beauty and the Beast",
            "Splash Mountain",
            "Big Thunder Mountain",
            "Enchanted Tiki Room",
            "Pirates of the Caribbean",
        ],
    },
    {
        "id": "disney-airbnb",
        "kind": "stay",
        "title": "Wooden House near Tokyo Disney",
        "subtitle": "Airbnb · check in from 4:00pm",
        "date": "2026-11-19",
        "time": "",
        "endTime": "10:00",
        "endDate": "2026-11-21",
        "booking": "",
        "detail": "$306.76 AUD",
        "cost": 306.76,
        "currency": "AUD",
        "phone": "+81 90-8486-9874",
        "notes": "Maihama Station is the closest, about a 15-minute walk. Checkout is 10:00am Saturday 21 Nov.",
        "link": "https://www.google.com/maps/search/?api=1&query=Benten+1-15-2+Urayasu+Chiba+279-0026",
        "linkLabel": "Address",
        "steps": [
            "Call the host before you arrive: +81 90-8486-9874.",
            "Address: Benten 1 Chome-15-2, Urayasu, Chiba 279-0026.",
            "Check in from 4:00pm on Thu 19 Nov. Total paid $306.76 AUD.",
            "Checkout is 10:00am on Sat 21 Nov.",
        ],
    },
    {
        "id": "disney-sea",
        "kind": "disney",
        "title": "Disney Sea",
        "subtitle": "",
        "date": "2026-11-20",
        "time": "",
        "endTime": "",
        "endDate": "",
        "booking": "A00626624710",
        "detail": "2 adults",
        "notes": "Show the QR from the confirmation email or the Tokyo Disney Resort app.",
        "link": "https://www.google.com/maps/search/?api=1&query=Tokyo+DisneySea",
        "linkLabel": "Park",
        "steps": [
            "Indiana Jones",
            "Journey to the Center of the Earth",
            "Lantern Festival",
            "Peter Pan",
            "Frozen Journey",
        ],
    },
    {
        "id": "eurasia-thu",
        "kind": "onsen",
        "title": "Spa & Hotel Maihama Eurasia",
        "subtitle": "Optional evening",
        "date": "2026-11-19",
        "time": "",
        "endTime": "",
        "endDate": "",
        "booking": "",
        "detail": "¥1,200–¥2,800 each",
        "phone": "+81 47-351-4126",
        "notes": "Day visit from the wooden house. The price depends on the time and what you add.",
        "link": "https://www.google.com/maps/dir/?api=1&origin=Benten+1-chome-15-2+Urayasu+Chiba+279-0026&destination=13-20+Chidori+Urayasu+Chiba+279-0032&travelmode=walking",
        "linkLabel": "Directions",
        "site": "https://my-spa.jp/?wovn=en",
        "siteLabel": "Hotel",
        "steps": [
            "Optional. Walk from the wooden house to JR Maihama Station, about 15 minutes.",
            "At the South Exit, find bus stop C-3. There is no hotel sign. Wait by the pillar marked C-3.",
            "The free Maihama Eurasia shuttle takes about 7 minutes. From 6:00pm the station buses leave at :05, :25 and :45, then 11:05pm and 11:25pm. 11:25pm is the last.",
            "Address: 13-20 Chidori, Urayasu, Chiba 279-0032. Day use is about ¥1,200 to ¥2,800 each, depending on the time and what you add.",
            "The last shuttle back to Maihama leaves the hotel at 11:48pm. If a bus is full, wait for the next one.",
        ],
    },
    {
        "id": "eurasia-fri",
        "kind": "onsen",
        "title": "Spa & Hotel Maihama Eurasia",
        "subtitle": "Optional evening",
        "date": "2026-11-20",
        "time": "",
        "endTime": "",
        "endDate": "",
        "booking": "",
        "detail": "¥1,200–¥2,800 each",
        "phone": "+81 47-351-4126",
        "notes": "Day visit from the wooden house. The price depends on the time and what you add.",
        "link": "https://www.google.com/maps/dir/?api=1&origin=Benten+1-chome-15-2+Urayasu+Chiba+279-0026&destination=13-20+Chidori+Urayasu+Chiba+279-0032&travelmode=walking",
        "linkLabel": "Directions",
        "site": "https://my-spa.jp/?wovn=en",
        "siteLabel": "Hotel",
        "steps": [
            "Optional. Walk from the wooden house to JR Maihama Station, about 15 minutes.",
            "At the South Exit, find bus stop C-3. There is no hotel sign. Wait by the pillar marked C-3.",
            "The free Maihama Eurasia shuttle takes about 7 minutes. From 6:00pm the station buses leave at :05, :25 and :45, then 11:05pm and 11:25pm. 11:25pm is the last.",
            "Address: 13-20 Chidori, Urayasu, Chiba 279-0032. Day use is about ¥1,200 to ¥2,800 each, depending on the time and what you add.",
            "The last shuttle back to Maihama leaves the hotel at 11:48pm. If a bus is full, wait for the next one.",
        ],
    },
    {
        "id": "shinkansen-kyoto",
        "kind": "train",
        "title": "Shinkansen to Kyoto",
        "subtitle": "Tokaido Shinkansen · Tokyo to Kyoto",
        "date": "2026-11-21",
        "time": "",
        "endTime": "",
        "endDate": "",
        "booking": "",
        "detail": "About ¥14,000 each · not booked yet",
        "color": "#ffd7d3",
        "notes": "Checkout at the wooden house is 10:00am. Nozomi takes about 2 hours 15 minutes from Tokyo Station to Kyoto. You are staying at Onyado Nono Kyoto Shichijo.",
        "link": "https://www.google.com/maps/dir/?api=1&origin=Maihama+Station&destination=Kyoto+Station&travelmode=transit",
        "linkLabel": "Train map",
        "steps": [
            "Not booked yet. Seats can be bought at a JR ticket office, at a green ticket machine, or on the Smart EX app.",
            "From the wooden house, walk to JR Maihama Station, about 15 minutes, then take the Keiyo Line to Tokyo Station. About 20 minutes.",
            "At Tokyo Station follow the Tokaido Shinkansen signs (東海道新幹線). Those gates are on the Yaesu side.",
            "Nozomi trains to Kyoto leave every few minutes, so there is no need to pick one in advance. A reserved seat is about ¥14,000 each, unreserved is a little less.",
            "The ride is about 2 hours 15 minutes. Sit on the right for Mount Fuji, about 40 minutes in.",
            "You sleep at Onyado Nono Kyoto Shichijo, about a 7-minute walk from Kyoto Station. Steps are on that stay.",
        ],
    },
    {
        "id": "kyoto-accom",
        "kind": "stay",
        "title": "Onyado Nono Kyoto Shichijo",
        "subtitle": "Booking.com · Standard Queen, breakfast",
        "date": "2026-11-21",
        "time": "",
        "endTime": "",
        "endDate": "2026-11-24",
        "booking": "5632.917.697",
        "detail": "Pin 8756",
        "cost": 1579,
        "currency": "AUD",
        "phone": "+81 75-365-5489",
        "notes": "491 Zaimokucho, Shimogyo-ku, Kyoto 600-8146. Check-in 3:00pm–midnight, checkout 11:00am. Breakfast included. Natural hot spring. Shoes off in the hotel.",
        "fileName": "Booking #5632917697.pdf",
        "link": "https://www.google.com/maps/search/?api=1&query=Onyado+Nono+Kyoto+Shichijo+Zaimokucho+491",
        "linkLabel": "Address",
        "site": "https://dormy-hotels.com/dormyinn/hotels/nono_kyoto/",
        "siteLabel": "Hotel",
        "placesLabel": "Things to do",
        "steps": [
            "Shinkansen lands at Kyoto Station. Use the Hachijo (south) side, then walk about 7 minutes to the hotel.",
            "Address: 491 Zaimokucho, Shimogyo-ku. Confirmation 5632.917.697, pin 8756. Guest name Gavin Macleod.",
            "Check-in is from 3:00pm. If you will arrive after 10:00pm, call the hotel first on +81 75-365-5489.",
            "Breakfast is included. The public bath is a natural hot spring. Tattoos may not be allowed in the bath.",
            "Kobo-ichi at To-ji is the same day as check-in, 21 Nov. Best before 11:00am. Vendors pack up from about 2:00pm.",
        ],
        "places": [
            {
                "name": "Nishiki Market",
                "note": "10:00am–6:00pm",
                "area": "Karasuma 2 stops to Shijo · 15–20 min",
                "site": "https://www.google.com/maps/search/?api=1&query=Nishiki+Market+Kyoto",
            },
            {
                "name": "Kobo-ichi at To-ji",
                "note": "21 Nov, before 2:00pm",
                "area": "Kintetsu 1 stop, or 20 min walk",
                "site": "https://www.google.com/maps/search/?api=1&query=To-ji+Temple+Kyoto",
            },
            {
                "name": "Arashiyama bamboo forest",
                "note": "Leave by 7:00am",
                "area": "JR Sagano Line to Saga-Arashiyama · 35–40 min",
                "site": "https://www.google.com/maps/search/?api=1&query=Arashiyama+Bamboo+Grove+Kyoto",
            },
            {
                "name": "Kiyomizu-dera",
                "note": "Opens 6:00am",
                "area": "Bus 206 from Shichijo Emae · 25–30 min",
                "site": "https://www.kiyomizudera.or.jp/en/",
            },
        ],
    },
    {
        "id": "shinkansen-tokyo",
        "kind": "train",
        "title": "Shinkansen to Tokyo",
        "subtitle": "Tokaido Shinkansen · Kyoto to Tokyo",
        "date": "2026-11-24",
        "time": "",
        "endTime": "",
        "endDate": "",
        "booking": "",
        "detail": "About ¥14,000 each · not booked yet",
        "color": "#ffd7d3",
        "notes": "Back to Tokyo after the three Kyoto nights. Nozomi takes about 2 hours 15 minutes. You are staying at Dormy Inn Akihabara from 24 to 27 Nov.",
        "link": "https://www.google.com/maps/dir/?api=1&origin=Kyoto+Station&destination=Tokyo+Station&travelmode=transit",
        "linkLabel": "Train map",
        "steps": [
            "Not booked yet. Seats can be bought at a JR ticket office, at a green ticket machine, or on the Smart EX app.",
            "At Kyoto Station the Shinkansen gates are on the south, Hachijo side. Follow the Tokaido Shinkansen signs (東海道新幹線).",
            "Nozomi trains to Tokyo leave every few minutes. A reserved seat is about ¥14,000 each, unreserved is a little less.",
            "The ride is about 2 hours 15 minutes. Going east, Mount Fuji is on the left.",
            "You sleep at Dormy Inn Akihabara. After Tokyo Station, take the Yamanote or Keihin-Tohoku two stops to Akihabara. Steps are on that stay.",
        ],
    },
    {
        "id": "tokyo-accom",
        "kind": "stay",
        "title": "Dormy Inn Akihabara",
        "subtitle": "Booking.com · Moderate Double, breakfast",
        "date": "2026-11-24",
        "time": "",
        "endTime": "",
        "endDate": "2026-11-27",
        "booking": "5177.959.056",
        "detail": "Pin 6827",
        "cost": 831,
        "currency": "AUD",
        "phone": "+81 3-5295-0012",
        "notes": "4-12-5 Sotokanda, Chiyoda-ku, Tokyo 101-0021. Check-in 3:00pm–midnight, checkout 11:00am. Breakfast included. City tax is paid at the hotel. Full payment at check-in.",
        "fileName": "Booking #5177959056.pdf",
        "link": "https://www.google.com/maps/search/?api=1&query=Dormy+Inn+Akihabara+4-12-5+Sotokanda+Chiyoda",
        "linkLabel": "Address",
        "site": "https://dormy-hotels.com/dormyinn/hotels/akihabara/",
        "siteLabel": "Hotel",
        "steps": [
            "Shinkansen lands at Tokyo Station. Stay inside the JR gates.",
            "Follow the Yamanote (山手線) or Keihin-Tohoku (京浜東北線) signs toward Ueno. Both stop at Akihabara.",
            "Ride two stops, Tokyo then Kanda then Akihabara, about 4 minutes. One Suica tap. There is no seat to reserve.",
            "At Akihabara (秋葉原) take the Electric Town Exit (電気街口). Dormy Inn is about a 5-minute walk north toward Suehirocho.",
            "Address: 4-12-5 Sotokanda, Chiyoda-ku. Confirmation 5177.959.056, pin 6827. Guest name Gavin Macleod.",
            "Ginza Line Suehirocho (末広町) is about a 1-minute walk if you use the metro instead.",
            "Check-in is from 3:00pm. If you will arrive after 10:00pm, call the hotel first on +81 3-5295-0012.",
            "Breakfast is included, 6:30am to 9:00am. The public bath is 3:00pm to 10:00am. Tattoos may not be allowed in the bath.",
        ],
        "places": [
            {"name": "Bar Record", "note": "Drinks", "area": "Ginza 8 Chome-7-20, Chuo City"},
        ],
    },
    {
        "id": "jl711-nrt-sin",
        "kind": "flight",
        "title": "Tokyo Narita to Singapore",
        "subtitle": "Japan Airlines JL711",
        "date": "2026-11-27",
        "time": "18:10",
        "endTime": "00:50",
        "endDate": "2026-11-28",
        "booking": "E4EDAY",
        "detail": "2 adults",
        "cost": 45584,
        "currency": "JPY",
        "notes": "Depart Narita Terminal 2 at 6:10pm. Land Singapore Changi Terminal 1 at 12:50am on Saturday 28 Nov.",
        "link": "https://www.google.com/maps/search/?api=1&query=Narita+Airport+Terminal+2",
        "linkLabel": "Airport",
    },
    {
        "id": "scoot-sin-dps",
        "kind": "flight",
        "title": "Singapore to Bali",
        "subtitle": "Scoot",
        "date": "2026-11-29",
        "time": "12:25",
        "endTime": "",
        "endDate": "",
        "booking": "",
        "detail": "",
        "notes": "Depart Singapore Changi at 12:25pm.",
        "link": "https://www.google.com/maps/search/?api=1&query=Singapore+Changi+Airport+Terminal+1",
        "linkLabel": "Airport",
    },
    {
        "id": "bali-accom",
        "kind": "stay",
        "title": "Villa Layla",
        "subtitle": "Booking.com · 2-bedroom villa, private pool",
        "date": "2026-11-29",
        "time": "",
        "endTime": "",
        "endDate": "2026-12-05",
        "booking": "5641.007.850",
        "detail": "Pin 6055",
        "cost": 977,
        "currency": "AUD",
        "phone": "+62 361 4740730",
        "notes": "Jalan Kayu Aya Gang Inti Kulit 117 C, Oberoi, Seminyak. Check-in 2:00pm–11:00pm, checkout 12:00pm. AUD 977 for 6 nights (villa 808, tax 81, service 88). No meals. Paid before arrival. No refund if cancelled. No parking. Free Wi-Fi.",
        "fileName": "Booking #5641007850.pdf",
        "link": "https://www.google.com/maps/search/?api=1&query=-8.68455,115.16142",
        "linkLabel": "Address",
        "steps": [
            "Scoot lands in Bali at 3:25pm on Sun 29 Nov. Check-in is open from 2:00pm until 11:00pm.",
            "Address: Jalan Kayu Aya Gang Inti Kulit 117 C, Oberoi, Seminyak 80361. Confirmation 5641.007.850, pin 6055. Guest name Gavin Macleod.",
            "Tell the villa your arrival time. Call +62 361 4740730 if the flight is late.",
            "Bring photo ID and the card used to book. The host charges that card before you arrive.",
            "Checkout is 12:00pm on Sat 5 Dec. The flight home leaves at 8:55pm. There is no parking at the villa.",
        ],
        "places": [
            {"name": "La Favela", "note": "Dinner", "site": "https://lafavelabali.com/"},
            {"name": "La Lucciola", "note": "Breakfast", "site": "https://lalucciolabali.com/"},
            {"name": "La Plancha", "note": "Drinks", "site": "https://laplancha-bali.com/"},
            {"name": "The Iron Fairies", "note": "Drinks", "site": "https://www.theironfairies.com/stores/the-iron-fairies-bali"},
        ],
    },
]



def _seed_ids() -> set[str]:
    return {str(item["id"]) for item in SEED}


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _color(value) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return text.lower()
    return ""


def _phone(value) -> str:
    text = _clean_text(value, 40)
    digits = re.sub(r"[^\d+]", "", text)
    if not re.fullmatch(r"\+\d{8,15}", digits):
        return ""
    return text


def _cost(value) -> float:
    text = re.sub(r"[,\s$]", "", str(value or "")).strip()
    if not text:
        return 0.0
    try:
        amount = round(float(text), 2)
    except ValueError:
        return 0.0
    if amount <= 0 or amount > 99_999_999:
        return 0.0
    return amount


def _currency(value) -> str:
    code = str(value or "").strip().upper()
    return code if code in _CURRENCIES else "AUD"


def _https_link(value) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme == "https" and parsed.netloc:
        return text[:400]
    return ""


def _category(raw: dict, kind: str) -> str:
    explicit = str(raw.get("category") or "").strip().lower()
    if explicit in _CATEGORIES:
        return explicit
    if kind == "stay":
        return "accom"
    text = " ".join([
        str(raw.get("title") or ""),
        str(raw.get("subtitle") or ""),
        str(raw.get("notes") or ""),
    ]).lower()
    if kind == "ticket" and "disney" in text:
        return "disney"
    if kind in {"other", "ticket"} and ("onsen" in text or "hot spring" in text):
        return "onsen"
    if kind in _CATEGORIES:
        return kind
    return ""


def _places(value) -> list[dict]:
    """Restaurants and bars listed under a stay."""
    rows = []
    if not isinstance(value, list):
        return rows
    for raw in value[:16]:
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("name"), 80)
        if not name:
            continue
        area = _clean_text(raw.get("area"), 90)
        site = _https_link(raw.get("site"))
        if not site and area:
            query = urlencode({"api": 1, "query": f"{name} {area}"})
            site = f"https://www.google.com/maps/search/?{query}"
        rows.append({
            "name": name,
            "note": _clean_text(raw.get("note"), 40),
            "area": area,
            "site": site,
        })
    return rows


def _public_item(raw: dict, *, seed: bool) -> dict:
    kind = str(raw.get("kind") or "other").strip().lower()
    if kind not in _KINDS:
        kind = "other"
    steps_in = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    steps = []
    for step in steps_in:
        cleaned = _clean_text(step, 240)
        if cleaned:
            steps.append(cleaned)
    return {
        "id": str(raw.get("id") or ""),
        "kind": kind,
        "category": _category(raw, kind),
        "title": _clean_text(raw.get("title"), 140),
        "subtitle": _clean_text(raw.get("subtitle"), 140),
        "date": str(raw.get("date") or "")[:10],
        "time": str(raw.get("time") or "")[:5],
        "endTime": str(raw.get("endTime") or "")[:5],
        "endDate": str(raw.get("endDate") or "")[:10],
        "booking": _clean_text(raw.get("booking"), 80),
        "detail": _clean_text(raw.get("detail"), 80),
        "notes": _clean_text(raw.get("notes"), 400),
        "phone": _phone(raw.get("phone")),
        "steps": steps[:12],
        "places": _places(raw.get("places")),
        "placesLabel": _clean_text(raw.get("placesLabel"), 40),
        "link": _https_link(raw.get("link")),
        "linkLabel": _clean_text(raw.get("linkLabel"), 40) or "Map",
        "site": _https_link(raw.get("site")),
        "siteLabel": _clean_text(raw.get("siteLabel"), 40) or "Website",
        "color": _color(raw.get("color")),
        "cost": _cost(raw.get("cost")),
        "currency": _currency(raw.get("currency")),
        "fileName": _booking_label(str(raw.get("id") or ""), raw.get("fileName")),
        "files": _attachments(raw),
        "photoStamp": _photo_stamp(str(raw.get("id") or "")),
        "seed": seed,
    }


INSURANCE_ID = "travel-insurance"

# World Nomads Standard policy WNAUS26517515, issued 29 Sep 2026.
def insurance_public() -> dict:
    return {
        "id": INSURANCE_ID,
        "title": "World Nomads travel insurance",
        "policy": "WNAUS26517515",
        "plan": "Standard",
        "travellers": "Jerry Silaban and Gavin Macleod",
        "dates": "17 Nov – 6 Dec 2026",
        "destinations": "Japan and Indonesia",
        "cost": 386.84,
        "currency": "AUD",
        "excess": 100.0,
        "medical": "$5,000,000",
        "fileName": _booking_label(INSURANCE_ID, "World Nomads certificate.pdf"),
        "emergency": [
            {"label": "Australia", "value": "1300 787 375", "href": "tel:1300787375"},
            {"label": "Worldwide", "value": "+61 2 8263 0470", "href": "tel:+61282630470"},
            {"label": "Also", "value": "+61 2 8292 1470", "href": "tel:+61282921470"},
            {"label": "Email", "value": "assist@worldnomads.com", "href": "mailto:assist@worldnomads.com"},
        ],
        "service": [
            {"label": "Australia", "value": "1300 787 375", "href": "tel:1300787375"},
            {"label": "Worldwide", "value": "+61 2 8263 0400", "href": "tel:+61282630400"},
            {"label": "Email", "value": "infoAUS@worldnomads.com", "href": "mailto:infoAUS@worldnomads.com"},
        ],
    }


def _payload() -> dict:
    return {
        "ok": True,
        "items": list_items(),
        "todos": list_todos(),
        "rates": RATES_PER_AUD,
        "insurance": insurance_public(),
    }


TODOS = [
    {"id": "tokyo-after-disney", "text": "Tokyo accom after Disney checkout"},
    {"id": "kyoto-train", "text": "Kyoto train"},
    {"id": "kyoto-accom", "text": "Kyoto accom"},
    {"id": "singapore-accom", "text": "Singapore accom"},
    {"id": "bali-accom", "text": "Bali accom"},
    {"id": "bali-flight-home", "text": "Bali flight back to Melbourne"},
]


def _raw_store() -> dict:
    path = _store_path()
    if not path.exists() or path.stat().st_size == 0:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _removed_todo_ids(payload: dict | None = None) -> list[str]:
    raw = (payload if payload is not None else _raw_store()).get("removedTodos")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def list_todos() -> list[dict]:
    removed = set(_removed_todo_ids())
    return [dict(row) for row in TODOS if row["id"] not in removed]


def remove_todo(todo_id: str) -> list[dict]:
    target = str(todo_id or "").strip()
    if target not in {row["id"] for row in TODOS}:
        raise ValueError("That item is already gone.")
    with _LOCK:
        added, edits = _read_store()
        removed = _removed_todo_ids()
        if target not in removed:
            removed.append(target)
        _write_store(added, edits, removed)
    return list_todos()


def _read_store() -> tuple[list[dict], dict]:
    path = _store_path()
    if not path.exists() or path.stat().st_size == 0:
        return [], {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    if not isinstance(payload, dict):
        return [], {}
    rows = payload.get("items") if isinstance(payload.get("items"), list) else []
    added = []
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip():
            added.append(_public_item(row, seed=False))
    edits_raw = payload.get("edits") if isinstance(payload.get("edits"), dict) else {}
    edits = {}
    for key, row in edits_raw.items():
        if isinstance(row, dict):
            edits[str(key)] = row
    return added, edits


def _write_store(items: list[dict], edits: dict, removed_todos: list[str] | None = None) -> None:
    if removed_todos is None:
        removed_todos = _removed_todo_ids()
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"items": items, "edits": edits, "removedTodos": removed_todos},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def _merge_override(base: dict, override: dict | None) -> dict:
    merged = dict(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        # Saved edits predate some map links. An empty link is not a deletion.
        if key in {"link", "site", "phone", "steps", "places", "fileName", "booking", "detail"} and not value and base.get(key):
            continue
        if key == "cost" and not _cost(value) and _cost(base.get("cost")):
            continue
        if key == "currency" and not _cost(override.get("cost")) and _cost(base.get("cost")):
            continue
        if key == "linkLabel" and not override.get("link") and base.get("link"):
            continue
        if key == "siteLabel" and not override.get("site") and base.get("site"):
            continue
        merged[key] = value
    merged["id"] = base.get("id")
    return merged


def list_items() -> list[dict]:
    with _LOCK:
        added, edits = _read_store()
    added = [row for row in added if row["id"] not in _seed_ids()]
    seeded = []
    for row in SEED:
        merged = _merge_override(row, edits.get(str(row["id"])))
        seeded.append(_public_item(merged, seed=True))
    return _sorted(seeded + added)


def _sorted(items: list[dict]) -> list[dict]:
    def key(item: dict) -> tuple:
        time = item.get("time") or "99:99"
        # Untimed onsen visits are evening options, so they sit after the other cards that day.
        evening = "1" if item.get("kind") == "onsen" and not item.get("time") else "0"
        return (item.get("date") or "9999-99-99", time, evening, item.get("title") or "")

    return sorted(items, key=key)


def _parse_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError("Use a real date.") from None
    if parsed.year < 2024 or parsed.year > 2035:
        raise ValueError("That date is outside the trip.")
    return parsed.isoformat()


def _parse_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _TIME_RE.fullmatch(text):
        raise ValueError("Use a time like 09:30.")
    return text


def _fields_from_body(body: dict, existing: dict | None) -> dict:
    title = _clean_text(body.get("title"), 140)
    if not title:
        raise ValueError("Add a name for this booking.")
    kind = str(body.get("kind") or "other").strip().lower()
    if kind not in _KINDS:
        kind = "other"
    start = _parse_date(str(body.get("date") or ""))
    if not start:
        raise ValueError("Add a date.")
    end = _parse_date(str(body.get("endDate") or ""))
    if end and end < start:
        raise ValueError("The end date is before the start date.")
    base = dict(existing or {})
    base.update({
        "kind": kind,
        "title": title,
        "date": start,
        "time": _parse_time(str(body.get("time") or "")),
        "endTime": _parse_time(str(body.get("endTime") or "")),
        "endDate": end,
        "booking": body.get("booking"),
        "detail": body.get("detail"),
        "notes": body.get("notes"),
        "color": body.get("color"),
        "cost": body.get("cost"),
        "currency": body.get("currency"),
    })
    return base


def add_item(body: dict) -> list[dict]:
    item = _public_item(_fields_from_body(body, {"id": uuid.uuid4().hex}), seed=False)
    with _LOCK:
        added, edits = _read_store()
        added = [row for row in added if row["id"] not in _seed_ids()]
        if len(added) >= 200:
            raise ValueError("The itinerary is full. Remove something first.")
        added.append(item)
        _write_store(added, edits)
    return list_items()


def update_item(body: dict) -> list[dict]:
    item_id = str(body.get("id") or "").strip()
    if not item_id:
        raise ValueError("That booking could not be found.")
    with _LOCK:
        added, edits = _read_store()
        added = [row for row in added if row["id"] not in _seed_ids()]
        if item_id in _seed_ids():
            current = _merge_override(next(row for row in SEED if row["id"] == item_id), edits.get(item_id))
            saved = _public_item(_fields_from_body(body, current), seed=True)
            edits[item_id] = {key: value for key, value in saved.items() if key != "seed"}
        else:
            match = next((row for row in added if row["id"] == item_id), None)
            if match is None:
                raise ValueError("That booking could not be found.")
            saved = _public_item(_fields_from_body(body, match), seed=False)
            added = [saved if row["id"] == item_id else row for row in added]
        _write_store(added, edits)
    return list_items()


def delete_item(item_id: str) -> list[dict]:
    target = str(item_id or "").strip()
    if not target or target in _seed_ids():
        raise ValueError("That booking stays on the itinerary.")
    with _LOCK:
        added, edits = _read_store()
        added = [row for row in added if row["id"] not in _seed_ids()]
        kept = [row for row in added if row["id"] != target]
        if len(kept) == len(added):
            raise ValueError("That booking is already gone.")
        edits.pop(target, None)
        _write_store(kept, edits)
    return list_items()


class _RateLimit(Exception):
    pass


def _map_places(link: str) -> tuple[str, str, str]:
    params = parse_qs(urlparse(str(link or "")).query)

    def one(key: str) -> str:
        values = params.get(key) or []
        return " ".join(str(values[0] or "").split()) if values else ""

    return one("query"), one("destination"), one("origin")


def _is_address(text: str) -> bool:
    return bool(_ADDRESS_RE.search(str(text or "")))


def _locality(text: str) -> str:
    if not _is_address(text):
        return ""
    cleaned = re.sub(r"\b\d{3}-?\d{4}\b", " ", text)
    cleaned = re.sub(r"\d+(?:-\d+)+", " ", cleaned)
    cleaned = re.sub(r"\b\d+\b", " ", cleaned)
    return " ".join(cleaned.split())


def _route_end(title: str) -> str:
    parts = re.split(r"\s+to\s+", str(title or ""), flags=re.IGNORECASE)
    if len(parts) < 2:
        return ""
    return " ".join(parts[-1].split())


def _add_query(queries: list[str], value: str) -> None:
    text = " ".join(str(value or "").split())
    if text and text not in queries:
        queries.append(text)


def _photo_queries(item: dict) -> list[str]:
    link_query, destination, origin = _map_places(str(item.get("link") or ""))
    title = str(item.get("title") or "")
    subtitle = str(item.get("subtitle") or "").split("·")[0].strip()
    kind = str(item.get("kind") or "")
    queries: list[str] = []
    if kind == "flight":
        _add_query(queries, link_query)
        _add_query(queries, _route_end(title))
    elif kind == "train":
        _add_query(queries, "" if _is_address(destination) else destination)
        _add_query(queries, subtitle)
        _add_query(queries, _route_end(title))
        _add_query(queries, "" if _is_address(origin) else origin)
    else:
        _add_query(queries, title)
        _add_query(queries, "" if _is_address(link_query) else link_query)
        _add_query(queries, "" if _is_address(destination) else destination)
        _add_query(queries, _locality(link_query or destination or origin))
        for extra in _extra_places(item):
            _add_query(queries, extra)
    return queries[:5]


def _extra_places(item: dict) -> list[str]:
    blob = " ".join([
        str(item.get("title") or ""),
        str(item.get("subtitle") or ""),
        str(item.get("notes") or ""),
        str(item.get("link") or ""),
    ]).lower()
    extras = []
    if "akihabara" in blob:
        extras.append("Akihabara")
    if "kyoto" in blob:
        extras.append("Kyoto Station")
    if "maihama" in blob:
        extras.append("Maihama")
    if "shinjuku" in blob:
        extras.append("Shinjuku")
    if "urayasu" in blob:
        extras.append("Urayasu")
    return extras


def _distinctive(text: str) -> list[str]:
    words = []
    for word in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if len(word) < 3 or word in _STOP_WORDS or word in _GENERIC_WORDS:
            continue
        words.append(word)
    return words


def _place_match(query: str, title: str) -> bool:
    wanted = _distinctive(query)
    have = set(_distinctive(title))
    if not wanted:
        return False
    if not any(word in have for word in wanted if len(word) == max(len(item) for item in wanted)):
        return False
    hits = [word for word in wanted if word in have]
    return len(hits) * 2 >= len(wanted)


def _pause_lookup() -> None:
    global _LAST_LOOKUP
    wait = 1.1 - (time.monotonic() - _LAST_LOOKUP)
    if wait > 0:
        time.sleep(wait)
    _LAST_LOOKUP = time.monotonic()


def _get_json(url: str) -> dict:
    _pause_lookup()
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise _RateLimit()
        return {}
    except (OSError, json.JSONDecodeError, TimeoutError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _wiki_thumb(query: str) -> str:
    url = "https://en.wikipedia.org/w/rest.php/v1/search/title?" + urlencode({"q": query, "limit": "3"})
    pages = _get_json(url).get("pages")
    if not isinstance(pages, list):
        return ""
    for page in pages:
        if not isinstance(page, dict) or not _place_match(query, str(page.get("title") or "")):
            continue
        thumb = page.get("thumbnail") if isinstance(page.get("thumbnail"), dict) else {}
        raw = str(thumb.get("url") or "")
        if raw.startswith("//"):
            raw = "https:" + raw
        if not raw.startswith("https://"):
            continue
        return re.sub(r"/\d+px-", "/330px-", raw, count=1)
    return ""


def _smaller_image(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "upload.wikimedia.org" or "/wikipedia/commons/" not in parsed.path:
        return url
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "wikipedia" or parts[1] != "commons":
        return url
    name = parts[-1]
    digest = "/".join(parts[2:-1])
    return f"https://thumb.wikimedia.org/wikipedia/commons/thumb/{digest}/{name}/330px-{name}"


def _openverse_thumb(query: str) -> str:
    url = "https://api.openverse.org/v1/images/?" + urlencode({"q": query, "page_size": "5"})
    results = _get_json(url).get("results")
    if not isinstance(results, list):
        return ""
    for row in results:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        if title and not _place_match(query, title):
            continue
        image = str(row.get("url") or "")
        if image.startswith("https://") and _host_ok(image):
            return _smaller_image(image)
    return ""


def _host_ok(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = ("wikimedia.org", "staticflickr.com", "openverse.org")
    return parsed.scheme == "https" and any(host == name or host.endswith("." + name) for name in allowed)


def _read_image(url: str) -> tuple[bytes, str] | None:
    current = url
    for _ in range(4):
        if not _host_ok(current):
            return None
        _pause_lookup()
        request = urllib.request.Request(
            current,
            headers={"User-Agent": _UA, "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*"},
        )
        try:
            opener = urllib.request.build_opener(_KeepRedirect)
            with opener.open(request, timeout=20) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                data = response.read(1_500_001)
                final = response.geturl()
        except (OSError, urllib.error.URLError, TimeoutError):
            return None
        if not _host_ok(final) or len(data) < 32 or len(data) > 1_500_000:
            return None
        if content_type.startswith("image/") and content_type != "image/svg+xml":
            return data, content_type
        return None
    return None


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _photo_paths(key: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    folder = _data_dir() / "trip_photos"
    return folder / f"{digest}.json", folder / f"{digest}.img"


_PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _local_photo_path(item_id: str) -> Path | None:
    if not _PHOTO_ID_RE.fullmatch(item_id):
        return None
    folder = _data_dir() / "trip_photos"
    for ext in _PHOTO_TYPES:
        path = folder / f"{item_id}{ext}"
        if path.is_file() and path.stat().st_size > 32:
            return path
    return None


def _local_photo(item_id: str) -> tuple[bytes, str] | None:
    path = _local_photo_path(item_id)
    if path is None:
        return None
    return path.read_bytes(), _PHOTO_TYPES[path.suffix.lower()]


def _photo_stamp(item_id: str) -> str:
    path = _local_photo_path(item_id)
    if path is None:
        return ""
    return str(int(path.stat().st_mtime))


def _photo_cache_get(key: str) -> tuple[bytes, str] | False | None:
    meta_path, image_path = _photo_paths(key)
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("empty"):
        age = time.time() - float(meta.get("at") or 0)
        if age > 12 * 60 * 60:
            return None
        return False
    if not image_path.exists():
        return None
    data = image_path.read_bytes()
    if not data:
        return None
    return data, str(meta.get("type") or "image/jpeg")


def _photo_cache_put(key: str, image: tuple[bytes, str]) -> None:
    meta_path, image_path = _photo_paths(key)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(image[0])
    meta_path.write_text(json.dumps({"type": image[1]}), encoding="utf-8")


def _photo_cache_empty(key: str) -> None:
    meta_path, _image_path = _photo_paths(key)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"empty": True, "at": time.time()}), encoding="utf-8")


def _find_photo_url(queries: list[str]) -> str:
    limited = False
    for query in queries:
        try:
            found = _wiki_thumb(query)
        except _RateLimit:
            limited = True
            break
        if found:
            return found
    for query in queries[:2]:
        try:
            found = _openverse_thumb(query)
        except _RateLimit:
            limited = True
            break
        if found:
            return found
    if limited:
        raise _RateLimit()
    return ""


def _photo_for_item(item: dict) -> tuple[bytes, str] | None:
    local = _local_photo(str(item.get("id") or ""))
    if local:
        return local
    queries = _photo_queries(item)
    if not queries:
        return None
    key = "\n".join(queries)
    cached = _photo_cache_get(key)
    if cached is not None:
        return cached or None
    with _PHOTO_LOCK:
        cached = _photo_cache_get(key)
        if cached is not None:
            return cached or None
        try:
            url = _find_photo_url(queries)
        except _RateLimit:
            return None
        if not url:
            _photo_cache_empty(key)
            return None
        image = _read_image(url)
        if not image:
            return None
        _photo_cache_put(key, image)
        return image


def _photo(handler, path: str) -> int:
    if not unlocked(handler.headers.get("Cookie", "")):
        handler.send_error(HTTPStatus.UNAUTHORIZED)
        return HTTPStatus.UNAUTHORIZED
    item_id = path.removeprefix("/trip/photo/").strip("/")
    if not _PHOTO_ID_RE.fullmatch(item_id):
        handler.send_error(HTTPStatus.NOT_FOUND)
        return HTTPStatus.NOT_FOUND
    match = next((row for row in list_items() if row.get("id") == item_id), None)
    if not match:
        handler.send_error(HTTPStatus.NOT_FOUND)
        return HTTPStatus.NOT_FOUND
    try:
        image = _photo_for_item(match)
    except Exception:
        image = None
    if not image:
        handler.send_error(HTTPStatus.NOT_FOUND)
        return HTTPStatus.NOT_FOUND
    body, content_type = image
    handler._send_bytes(
        body,
        status=HTTPStatus.OK,
        content_type=content_type,
        cache_control="private, no-store",
    )
    return HTTPStatus.OK


def _booking_file(handler, path: str) -> int:
    if not unlocked(handler.headers.get("Cookie", "")):
        handler.send_error(HTTPStatus.UNAUTHORIZED)
        return HTTPStatus.UNAUTHORIZED
    item_id = path.removeprefix("/trip/file/").strip("/")
    if not _PHOTO_ID_RE.fullmatch(item_id):
        handler.send_error(HTTPStatus.NOT_FOUND)
        return HTTPStatus.NOT_FOUND
    match = None
    for row in list_items():
        if row.get("id") == item_id and row.get("fileName"):
            match = row
            break
        for attachment in row.get("files") or []:
            if isinstance(attachment, dict) and attachment.get("id") == item_id and attachment.get("fileName"):
                match = attachment
                break
        if match is not None:
            break
    if match is None and item_id == INSURANCE_ID:
        cover = insurance_public()
        if cover.get("fileName"):
            match = cover
    target = _booking_pdf(item_id)
    if match is None or not target.is_file():
        handler.send_error(HTTPStatus.NOT_FOUND)
        return HTTPStatus.NOT_FOUND
    download_name = _pdf_download_name(str(match.get("fileName") or ""))
    handler._send_bytes(
        target.read_bytes(),
        status=HTTPStatus.OK,
        content_type="application/pdf",
        cache_control="private, no-store",
        extra_headers={"Content-Disposition": f'inline; filename="{download_name}"'},
    )
    return HTTPStatus.OK


def _moved_to(path: str) -> str:
    """Where the trip lives once it has its own Railway service.

    Only the ema app reads this. Set TRIP_REDIRECT_URL on the ema service (for
    example https://trip-production.up.railway.app) after the trip service has
    its data. Unset, ema keeps serving /trip exactly as before. The trip service
    itself sets TRIP_STANDALONE, so it never redirects to itself.
    """
    if str(os.environ.get("TRIP_STANDALONE", "") or "").strip():
        return ""
    base = str(os.environ.get("TRIP_REDIRECT_URL", "") or "").strip().rstrip("/")
    if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
        return ""
    if path != "/trip" and not path.startswith("/trip/"):
        return ""
    return base + path


def _send_moved(handler, target: str, *, post: bool) -> int:
    if post:
        handler._send_json(
            {"ok": False, "error": "The trip page has moved. Open it at its new address.", "movedTo": target},
            status=HTTPStatus.GONE,
        )
        return HTTPStatus.GONE
    handler._send_bytes(
        b"",
        status=HTTPStatus.FOUND,
        content_type="text/plain; charset=utf-8",
        cache_control="no-store",
        extra_headers={"Location": target},
    )
    return HTTPStatus.FOUND


def handle_get(handler, path: str) -> int | None:
    moved = _moved_to(path)
    if moved:
        return _send_moved(handler, moved, post=False)
    if path == "/trip":
        handler._serve_file(PAGE, "text/html; charset=utf-8")
        return HTTPStatus.OK
    if path.startswith("/trip/photo/"):
        return _photo(handler, path)
    if path.startswith("/trip/file/"):
        return _booking_file(handler, path)
    if path == "/trip/api/deploy":
        return _deploy_status(handler)
    if path != "/trip/api":
        return None
    if not unlocked(handler.headers.get("Cookie", "")):
        handler._send_json({"ok": False, "locked": True}, status=HTTPStatus.UNAUTHORIZED)
        return HTTPStatus.UNAUTHORIZED
    handler._send_json(_payload())
    return HTTPStatus.OK


def handle_post(handler, path: str) -> int | None:
    moved = _moved_to(path)
    if moved:
        return _send_moved(handler, moved, post=True)
    if path == "/trip/api/reset-password":
        return _reset_password(handler)
    if path == "/trip/api/unlock":
        return _unlock(handler)
    if path == "/trip/api/items":
        return _add(handler)
    if path == "/trip/api/items/update":
        return _update(handler)
    if path == "/trip/api/items/delete":
        return _delete(handler)
    if path == "/trip/api/todos/delete":
        return _delete_todo(handler)
    if path == "/trip/api/lock":
        return _lock(handler)
    if path == "/trip/api/deploy":
        return _deploy_start(handler)
    return None


def _reset_password(handler) -> int:
    # The body is ignored. An address or password in it cannot change the reset.
    handler._read_json()
    try:
        reset_password()
    except ResetError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, status=exc.status)
        return exc.status
    handler._send_json({
        "ok": True,
        "message": "A new password was emailed. The previous one no longer works.",
    })
    return HTTPStatus.OK


def _unlock(handler) -> int:
    body = handler._read_json()
    if not _password_accepted():
        handler._send_json(
            {"ok": False, "error": "This page is locked until a host password is set."},
            status=HTTPStatus.FORBIDDEN,
        )
        return HTTPStatus.FORBIDDEN
    if not _word_matches(str(body.get("password") or "")):
        if _too_many_tries():
            handler._send_json({"ok": False, "error": "Too many tries. Wait a few minutes."}, status=429)
            return 429
        handler._send_json({"ok": False, "error": "That word did not match."}, status=HTTPStatus.UNAUTHORIZED)
        return HTTPStatus.UNAUTHORIZED
    handler._send_json(
        _payload(),
        extra_headers={"Set-Cookie": cookie_header(secure=handler._cookie_secure())},
    )
    return HTTPStatus.OK


def _lock(handler) -> int:
    handler._send_json(
        {"ok": True, "locked": True},
        extra_headers={"Set-Cookie": cookie_header(secure=handler._cookie_secure(), clear=True)},
    )
    return HTTPStatus.OK


_COPY_LOCK = threading.Lock()
_COPY_JOB = {"running": False, "succeeded": None, "error": ""}


def _railway_exe() -> Path:
    local = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
    exe = Path(local) / "railway-cli" / "railway.exe" if local else Path()
    if exe.is_file():
        return exe
    found = shutil.which("railway")
    if found:
        return Path(found)
    raise RuntimeError("The Railway CLI is not on this PC.")


# The PC-only "copy itinerary to production" button targets these. They default to
# the ema service so nothing changes until the trip service takes over.
def _prod_service() -> str:
    return str(os.environ.get("TRIP_PROD_SERVICE", "") or "ema").strip() or "ema"


def _prod_data_dir() -> str:
    default = "/data" if _prod_service() != "ema" else "/data/ema-railway-data"
    raw = str(os.environ.get("TRIP_PROD_DATA_DIR", "") or default).strip().rstrip("/")
    return raw or default


def _prod_trip_url() -> str:
    override = str(os.environ.get("TRIP_PROD_URL", "") or "").strip().rstrip("/")
    if override:
        return override if override.endswith("/trip") else f"{override}/trip"
    try:
        import railway_deploy
    except ImportError:
        return ""

    host = str(railway_deploy.hosted_targets(_data_dir()).get("prod") or "").rstrip("/")
    return f"{host}/trip" if host else ""


def _store_for_prod() -> dict:
    payload = _raw_store()
    if not isinstance(payload, dict):
        payload = {}
    bali = next((dict(row) for row in SEED if row["id"] == "bali-accom"), None)
    edits = dict(payload.get("edits") or {})
    if bali:
        edits["bali-accom"] = bali
    removed = [str(item) for item in (payload.get("removedTodos") or []) if str(item).strip()]
    if "bali-accom" not in removed:
        removed.append("bali-accom")
    payload["edits"] = edits
    payload["removedTodos"] = removed
    return payload


def _ssh_prod(code: str, payload: bytes, *args: str) -> None:
    proc = subprocess.run(
        [str(_railway_exe()), "ssh", "-s", _prod_service(), "-e", "production", "--", "python", "-c", code, *args],
        input=payload,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise RuntimeError(detail[-400:] or "The copy to production failed.")


def _copy_itinerary_to_prod() -> None:
    error = ""
    succeeded = False
    try:
        store = json.dumps(_store_for_prod(), ensure_ascii=False).encode("utf-8")
        _ssh_prod(
            "import json,sys\nfrom pathlib import Path\n"
            "path=Path(sys.argv[1],'trip_itinerary.json')\n"
            "path.write_text(json.dumps(json.loads(sys.stdin.read()),ensure_ascii=False,indent=2),encoding='utf-8')\n",
            store,
            _prod_data_dir(),
        )
        folder = _data_dir() / "trip_files"
        names = sorted(path.name for path in folder.glob("*.pdf")) if folder.is_dir() else []
        for name in names:
            _ssh_prod(
                "import base64,sys\nfrom pathlib import Path\n"
                "Path(sys.argv[2],'trip_files').mkdir(parents=True,exist_ok=True)\n"
                "Path(sys.argv[2],'trip_files',sys.argv[1]).write_bytes(base64.b64decode(sys.stdin.read().strip()))\n",
                base64.b64encode((folder / name).read_bytes()),
                name,
                _prod_data_dir(),
            )
        succeeded = True
    except Exception as exc:
        error = str(exc).strip() or "The copy to production failed."
    with _COPY_LOCK:
        _COPY_JOB["running"] = False
        _COPY_JOB["succeeded"] = succeeded
        _COPY_JOB["error"] = "" if succeeded else error


def _promote_module():
    """The trip copy of railway_deploy (app/railway_deploy.py), only in the standalone trip service.

    Imported the same way as secret_box: app/ is on sys.path. Inside ema the
    module of that name belongs to Lost Miniatures, so it is never used there.
    """
    if os.environ.get("TRIP_STANDALONE") != "1":
        return None
    try:
        import railway_deploy
    except ImportError:
        return None
    return railway_deploy if hasattr(railway_deploy, "can_promote") else None


def _project_root() -> Path:
    # app/trip/routes.py -> repository root (parent of app/).
    return Path(__file__).resolve().parents[2]


def _app_environment() -> str:
    module = _promote_module()
    if module is not None:
        return module.app_environment()
    explicit = str(os.environ.get("DLM_ENV", "") or "").strip().upper()
    if explicit in {"DEV", "DEVELOPMENT"}:
        return "DEV"
    if explicit in {"PROD", "PRODUCTION"}:
        return "PROD"
    name = str(os.environ.get("RAILWAY_ENVIRONMENT_NAME", "") or "").strip().lower()
    if name in {"development", "dev", "staging"}:
        return "DEV"
    return "PROD" if _on_railway() else "LOCAL"


def _promote_payload(module) -> dict:
    state = module.status(_data_dir())
    prod = str(state.get("prodUrl") or "").rstrip("/")
    prod_trip = f"{prod}/trip" if prod and not prod.endswith("/trip") else prod
    state.update({
        "mode": "promote",
        "url": prod_trip,
        "prodUrl": prod_trip,
        "published": published_stamp(),
    })
    return state


def _deploy_payload() -> dict:
    module = _promote_module()
    if module is not None and module.can_promote():
        return _promote_payload(module)
    with _COPY_LOCK:
        job = dict(_COPY_JOB)
    local = not _on_railway()
    url = "" if not local else _prod_trip_url()
    return {
        "ok": True,
        "mode": "itinerary" if local else "none",
        "local": local,
        "environment": _app_environment(),
        "promoteProd": False,
        "promoteReady": False,
        "url": url,
        "prodUrl": url,
        "running": bool(job["running"]) if local else False,
        "succeeded": job["succeeded"] if local else None,
        "error": str(job["error"] or "") if local else "",
        "log": "",
        "published": published_stamp(),
    }


def _deploy_status(handler) -> int:
    if not _require_unlock(handler):
        return HTTPStatus.UNAUTHORIZED
    handler._send_json(_deploy_payload())
    return HTTPStatus.OK


def _deploy_start(handler) -> int:
    if not _require_unlock(handler):
        return HTTPStatus.UNAUTHORIZED
    handler._read_json()  # Body is ignored; the environment decides what a deploy means.
    if _on_railway():
        module = _promote_module()
        if module is None or not module.can_promote():
            handler._send_json(
                {"ok": False, "error": "This is production. Deploy to production from the hosted DEV trip site."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return HTTPStatus.BAD_REQUEST
        try:
            module.start(_project_root(), _data_dir(), target="prod")
        except module.RailwayDeployError as exc:
            payload = _promote_payload(module)
            payload.update({"ok": False, "error": str(exc)})
            handler._send_json(payload, status=HTTPStatus.BAD_REQUEST)
            return HTTPStatus.BAD_REQUEST
        handler._send_json(_promote_payload(module))
        return HTTPStatus.OK
    with _COPY_LOCK:
        if not _COPY_JOB["running"]:
            _COPY_JOB["running"] = True
            _COPY_JOB["succeeded"] = None
            _COPY_JOB["error"] = ""
            threading.Thread(target=_copy_itinerary_to_prod, name="trip-copy-prod", daemon=True).start()
    handler._send_json(_deploy_payload())
    return HTTPStatus.OK


def _require_unlock(handler) -> bool:
    if unlocked(handler.headers.get("Cookie", "")):
        return True
    handler._send_json({"ok": False, "locked": True}, status=HTTPStatus.UNAUTHORIZED)
    return False


def _add(handler) -> int:
    if not _require_unlock(handler):
        return HTTPStatus.UNAUTHORIZED
    try:
        items = add_item(handler._read_json())
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return HTTPStatus.BAD_REQUEST
    handler._send_json({"ok": True, "items": items})
    return HTTPStatus.OK


def _update(handler) -> int:
    if not _require_unlock(handler):
        return HTTPStatus.UNAUTHORIZED
    try:
        items = update_item(handler._read_json())
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return HTTPStatus.BAD_REQUEST
    handler._send_json({"ok": True, "items": items})
    return HTTPStatus.OK


def _delete(handler) -> int:
    if not _require_unlock(handler):
        return HTTPStatus.UNAUTHORIZED
    try:
        items = delete_item(str(handler._read_json().get("id") or ""))
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return HTTPStatus.BAD_REQUEST
    handler._send_json({"ok": True, "items": items})
    return HTTPStatus.OK


def _delete_todo(handler) -> int:
    if not _require_unlock(handler):
        return HTTPStatus.UNAUTHORIZED
    try:
        todos = remove_todo(str(handler._read_json().get("id") or ""))
    except ValueError as exc:
        handler._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return HTTPStatus.BAD_REQUEST
    handler._send_json({"ok": True, "todos": todos})
    return HTTPStatus.OK


