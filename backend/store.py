"""In-memory mock data (doctors, slots, appointments) + event bus for the dashboard."""
import asyncio
import re
from datetime import date, timedelta
from itertools import count

WORD_DIGITS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
               "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
TIMES = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]

DOCTORS = [
    {"id": "d1", "name": "Dr. Ananya Sharma", "specialty": "Cardiology"},
    {"id": "d2", "name": "Dr. Rohan Mehta", "specialty": "Dermatology"},
    {"id": "d3", "name": "Dr. Priya Nair", "specialty": "Pediatrics"},
    {"id": "d4", "name": "Dr. Vikram Singh", "specialty": "Orthopedics"},
]

_ids = count(1)
appointments: list[dict] = []
_subscribers: list[asyncio.Queue] = []


def normalize_phone(raw: str) -> str:
    """'nine eight 7 6-5...' -> '98765...' (digits only)."""
    tokens = re.split(r"[\s,\-]+", str(raw).lower())
    out = "".join(WORD_DIGITS.get(t, t) for t in tokens)
    return re.sub(r"\D", "", out)


def _all_dates():
    return [(date.today() + timedelta(days=i)).isoformat() for i in range(1, 6)]


def _find_doctor(name=None, specialty=None):
    for d in DOCTORS:
        if name and name.lower().replace("dr.", "").strip() in d["name"].lower():
            return d
        if specialty and specialty.lower() in d["specialty"].lower():
            return d
    return None


def _booked(doctor_id, day, time):
    return any(a["doctor_id"] == doctor_id and a["date"] == day and a["time"] == time for a in appointments)


def free_slots(doctor_id, day=None):
    days = [day] if day else _all_dates()
    return {d: [t for t in TIMES if not _booked(doctor_id, d, t)] for d in days}


def snapshot():
    return {
        "doctors": [{**d, "slots": free_slots(d["id"])} for d in DOCTORS],
        "appointments": sorted(appointments, key=lambda a: (a["date"], a["time"])),
    }


# ---------- tool implementations (return plain dicts for the LLM) ----------

def check_availability(doctor_name=None, specialty=None, date_str=None):
    if date_str and date_str not in _all_dates():
        return {"error": f"Bookable dates are {_all_dates()[0]} to {_all_dates()[-1]}.", "valid_dates": _all_dates()}
    docs = [_find_doctor(doctor_name, specialty)] if (doctor_name or specialty) else DOCTORS
    if docs == [None]:
        return {"error": "No matching doctor.", "doctors": [f"{d['name']} ({d['specialty']})" for d in DOCTORS]}
    result = [{"doctor": d["name"], "specialty": d["specialty"], "available": free_slots(d["id"], date_str)} for d in docs]
    return {"results": result}


def book(phone, confirmed, doctor_name, date_str, time_str, patient_name=None):
    ph = normalize_phone(phone)
    if not confirmed:
        return {"error": "Phone number not confirmed. Read it back digit by digit and get a yes first."}
    if len(ph) < 10:
        return {"error": "Phone number looks incomplete; ask the caller to repeat all digits."}
    doc = _find_doctor(doctor_name)
    if not doc:
        return {"error": "Unknown doctor."}
    if date_str not in _all_dates() or time_str not in TIMES:
        return {"error": "Invalid date or time.", "valid_times": TIMES, "valid_dates": _all_dates()}
    if _booked(doc["id"], date_str, time_str):
        return {"error": "That slot is already taken.", "available": free_slots(doc["id"], date_str)}
    appt = {"id": f"A{next(_ids):03d}", "phone": ph, "patient": patient_name or "Caller", "doctor_id": doc["id"],
            "doctor": doc["name"], "specialty": doc["specialty"], "date": date_str, "time": time_str}
    appointments.append(appt)
    publish({"type": "state"})
    publish({"type": "action", "action": "booked", "appointment": appt})
    return {"status": "booked", "appointment": appt}


def cancel(phone, confirmed, appointment_id=None):
    ph = normalize_phone(phone)
    if not confirmed:
        return {"error": "Phone number not confirmed. Read it back digit by digit and get a yes first."}
    mine = [a for a in appointments if a["phone"] == ph]
    if not mine:
        return {"error": "No appointments found for that phone number."}
    if appointment_id:
        mine = [a for a in mine if a["id"] == appointment_id]
    elif len(mine) > 1:
        return {"error": "Multiple appointments; ask which one.", "appointments": mine}
    if not mine:
        return {"error": "Appointment id not found for this number."}
    appt = mine[0]
    appointments.remove(appt)
    publish({"type": "state"})
    publish({"type": "action", "action": "cancelled", "appointment": appt})
    return {"status": "cancelled", "appointment": appt}


# ---------- event bus (SSE) ----------

def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q):
    if q in _subscribers:
        _subscribers.remove(q)


def publish(event: dict):
    for q in list(_subscribers):
        q.put_nowait(event)
