from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("SMARTFLEET_DB", ROOT / "smartfleet.db"))
LOCK = threading.RLock()
SIMULATION_WAKE = threading.Event()
SIMULATION = {"running": False, "mode": "Recommend actions", "interval": 15}
DEMO_COST_WITH_REPLACEMENT = int(os.environ.get("SMARTFLEET_COST_WITH_REPLACEMENT", "1800"))
DEMO_COST_WITHOUT_REPLACEMENT = int(os.environ.get("SMARTFLEET_COST_WITHOUT_REPLACEMENT", "12000"))


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat()


@contextmanager
def connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_audit(db: sqlite3.Connection, event: str, entity_type: str, entity_id: str, details: dict) -> None:
    db.execute(
        "INSERT INTO audit_events(id, event, entity_type, entity_id, details, created_at) VALUES(?,?,?,?,?,?)",
        (str(uuid.uuid4()), event, entity_type, entity_id, json.dumps(details), stamp()),
    )


def seed() -> None:
    with LOCK, connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS vehicles (
          id TEXT PRIMARY KEY, registration TEXT NOT NULL, make TEXT NOT NULL, model TEXT NOT NULL,
          category TEXT NOT NULL, capacity INTEGER NOT NULL, location TEXT NOT NULL,
          latitude REAL, longitude REAL, energy INTEGER NOT NULL, energy_type TEXT NOT NULL,
          status TEXT NOT NULL, safety TEXT NOT NULL DEFAULT 'SAFE', service_status TEXT NOT NULL DEFAULT 'CLEAR',
          telemetry_at TEXT NOT NULL, current_booking TEXT, notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS bookings (
          id TEXT PRIMARY KEY, customer TEXT NOT NULL, phone TEXT DEFAULT '', vehicle_id TEXT,
          category TEXT NOT NULL, capacity INTEGER NOT NULL, pickup_location TEXT NOT NULL,
          pickup_at TEXT NOT NULL, return_at TEXT NOT NULL, status TEXT NOT NULL,
          energy_required INTEGER NOT NULL DEFAULT 30, created_at TEXT NOT NULL,
          FOREIGN KEY(vehicle_id) REFERENCES vehicles(id)
        );
        CREATE INDEX IF NOT EXISTS booking_vehicle_window ON bookings(vehicle_id, pickup_at, return_at, status);
        CREATE TABLE IF NOT EXISTS incidents (
          id TEXT PRIMARY KEY, type TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
          vehicle_id TEXT, booking_id TEXT, summary TEXT NOT NULL, evidence TEXT NOT NULL,
          recommendation TEXT NOT NULL, detected_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS incident_status ON incidents(status, severity);
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, operation TEXT NOT NULL,
          reason TEXT NOT NULL, vehicle_id TEXT, booking_id TEXT, proposed_vehicle_id TEXT,
          status TEXT NOT NULL, requested_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT,
          decision_reason TEXT, snapshot TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id TEXT PRIMARY KEY, recipient TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL,
          booking_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telemetry (
          id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, energy INTEGER NOT NULL, location TEXT NOT NULL,
          safety TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(vehicle_id) REFERENCES vehicles(id)
        );
        CREATE TABLE IF NOT EXISTS maintenance (
          id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, description TEXT NOT NULL,
          status TEXT NOT NULL, scheduled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_recommendations (
          id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, action TEXT NOT NULL,
          evidence TEXT NOT NULL, alternatives TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          id TEXT PRIMARY KEY, event TEXT NOT NULL, entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        if db.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]:
            approval = db.execute("SELECT id FROM approvals WHERE id='AP-207' AND status='PENDING'").fetchone()
            candidate = db.execute("SELECT * FROM vehicles WHERE id='VH-013'").fetchone()
            booking = db.execute("SELECT * FROM bookings WHERE id='BK-008'").fetchone()
            if approval and candidate and booking:
                snapshot = {"candidate_energy": candidate["energy"], "candidate_status": candidate["status"], "candidate_safety": candidate["safety"], "candidate_service": candidate["service_status"], "booking_vehicle": booking["vehicle_id"], "return_at": booking["return_at"]}
                db.execute("UPDATE approvals SET proposed_vehicle_id=?, snapshot=? WHERE id=?", (candidate["id"], json.dumps(snapshot), approval["id"]))
            maintenance_reasons = {"VH-001": "Scheduled battery system inspection", "VH-008": "High-voltage system diagnostic", "VH-015": "Routine drivetrain service", "VH-022": "Scheduled safety inspection"}
            for vehicle_id, description in maintenance_reasons.items():
                if not db.execute("SELECT 1 FROM maintenance WHERE vehicle_id=? AND status IN ('OPEN','BLOCKING')", (vehicle_id,)).fetchone():
                    db.execute("INSERT INTO maintenance VALUES(?,?,?,?,?)", (f"MT-{vehicle_id}", vehicle_id, description, "BLOCKING", stamp(now()+timedelta(days=1))))
            for vehicle in db.execute("SELECT id,energy,location,safety FROM vehicles").fetchall():
                db.execute("UPDATE telemetry SET energy=?,location=?,safety=? WHERE vehicle_id=? AND typeof(energy)!='integer'", (vehicle["energy"], vehicle["location"], vehicle["safety"], vehicle["id"]))
            return
        makes = [
            ("Tata", "Nexon EV", "SUV", "Electric"), ("Hyundai", "Creta", "SUV", "Fuel"),
            ("Maruti", "Brezza", "Compact", "Fuel"), ("Mahindra", "XUV 700", "SUV", "Fuel"),
            ("Kia", "Seltos", "Compact", "Fuel"), ("MG", "ZS EV", "SUV", "Electric"),
        ]
        hubs = [("Bengaluru", 12.9716, 77.5946), ("Chennai", 13.0827, 80.2707),
                ("Mumbai", 19.0760, 72.8777), ("Hyderabad", 17.3850, 78.4867)]
        vehicles = []
        for i in range(1, 25):
            make, model, category, energy_type = makes[(i - 1) % len(makes)]
            city, lat, lon = hubs[(i - 1) % len(hubs)]
            if i in (5, 17):
                city, lat, lon = hubs[0]
            status, safety, service = "AVAILABLE", "SAFE", "CLEAR"
            energy = 40 + (i * 17) % 58
            if i in (1, 8, 15, 22):
                status, service = "MAINTENANCE", "BLOCKING"
            if i in (4, 16):
                status, safety = "UNSAFE", "FAULT"
            if i in (6, 11, 21):
                status = "BOOKED"
            if i == 8:
                energy = 18
            vehicles.append((f"VH-{i:03}", f"KA-{i:02}-MN-{2200+i}", make, model, category,
                5 if category == "SUV" else 5, city, lat + (i % 5) * .002, lon + (i % 4) * .002,
                energy, energy_type, status, safety, service, stamp(now() - timedelta(minutes=(i % 9) + 1)), None,
                "Brake pressure variance detected across 3 readings." if i in (4, 16) else ""))
        db.executemany("""INSERT INTO vehicles(id,registration,make,model,category,capacity,location,latitude,longitude,
          energy,energy_type,status,safety,service_status,telemetry_at,current_booking,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vehicles)
        base = now().replace(second=0, microsecond=0)
        booking_rows = [
            ("BK-003", "Aarav Sharma", "+91 90000 10003", "VH-003", "Compact", 4, "Indiranagar hub", stamp(base-timedelta(hours=2)), stamp(base+timedelta(minutes=20)), "OVERDUE", 30, stamp(base-timedelta(days=1))),
            ("BK-006", "Kavya Rao", "+91 90000 10006", "VH-006", "SUV", 4, "Koramangala hub", stamp(base+timedelta(hours=1)), stamp(base+timedelta(hours=5)), "ACTIVE", 35, stamp(base-timedelta(days=1))),
            ("BK-008", "Ishaan Mehta", "+91 90000 10008", "VH-008", "SUV", 4, "Whitefield hub", stamp(base+timedelta(hours=3)), stamp(base+timedelta(hours=8)), "CONFIRMED", 50, stamp(base-timedelta(hours=8))),
            ("BK-011", "Mira Nair", "+91 90000 10011", "VH-011", "Compact", 4, "Bandra hub", stamp(base+timedelta(hours=2)), stamp(base+timedelta(hours=6)), "CONFIRMED", 30, stamp(base-timedelta(hours=6))),
            ("BK-014", "Rohan Das", "+91 90000 10014", "VH-014", "SUV", 4, "Adyar hub", stamp(base+timedelta(hours=6)), stamp(base+timedelta(hours=12)), "CONFIRMED", 30, stamp(base-timedelta(hours=4))),
            ("BK-018", "Anaya Kapoor", "+91 90000 10018", "VH-018", "SUV", 4, "Hitech City hub", stamp(base+timedelta(hours=9)), stamp(base+timedelta(hours=15)), "CONFIRMED", 35, stamp(base-timedelta(hours=2))),
            ("BK-021", "Dev Patel", "+91 90000 10021", "VH-021", "Compact", 4, "Powai hub", stamp(base+timedelta(hours=12)), stamp(base+timedelta(hours=17)), "CONFIRMED", 35, stamp(base-timedelta(hours=1))),
        ]
        db.executemany("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", booking_rows)
        for vehicle_id, booking_id in [("VH-003", "BK-003"), ("VH-006", "BK-006"), ("VH-008", "BK-008"), ("VH-011", "BK-011"), ("VH-014", "BK-014"), ("VH-018", "BK-018"), ("VH-021", "BK-021")]:
            db.execute("UPDATE vehicles SET current_booking=? WHERE id=?", (booking_id, vehicle_id))
        incident_rows = [
            ("INC-1042", "Overdue return", "HIGH", "INVESTIGATING", "VH-003", "BK-003", "Return window exceeded by 42 minutes at Indiranagar hub.", json.dumps(["Last telemetry: 12:42 PM", "Customer ETA: 1:20 PM", "Vehicle booked again at 2:00 PM"]), "Protect the next booking; compare eligible replacements and notify the customer.", stamp(base-timedelta(minutes=12)), None),
            ("INC-1041", "Safety fault", "CRITICAL", "DETECTED", "VH-016", None, "Brake pressure variance detected across 3 readings.", json.dumps(["Brake pressure variance: 18%", "Three consecutive readings", "Vehicle status: unsafe"]), "Keep vehicle blocked and escalate to a manager. Do not assign.", stamp(base-timedelta(minutes=24)), None),
            ("INC-1040", "Low battery", "MEDIUM", "AWAITING APPROVAL", "VH-008", "BK-008", "Battery below operating threshold before next booking.", json.dumps(["Battery: 18%", "Required for booking: 50%", "Pickup in 3 hours"]), "Reassign to an eligible SUV after manager approval.", stamp(base-timedelta(minutes=41)), None),
        ]
        db.executemany("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)", incident_rows)
        for incident in incident_rows:
            record_audit(db, "Incident detected", "incident", incident[0], {"type": incident[1], "severity": incident[2], "vehicle_id": incident[4], "booking_id": incident[5]})
        db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "AP-207", "INC-1040", "Reassign booking", "Energy below booking requirement; preserve confirmed pickup.",
            "VH-008", "BK-008", "VH-013", "PENDING", stamp(base-timedelta(minutes=20)), None, None, None,
            json.dumps({"candidate_energy": 87, "candidate_status": "AVAILABLE", "candidate_safety": "SAFE", "candidate_service": "CLEAR", "booking_vehicle": "VH-008", "return_at": booking_rows[2][8]})))
        notifications = [
            ("NT-018", "Aarav Sharma", "In-app", "Your vehicle handover is being coordinated by SmartFleet.", "BK-003", "QUEUED", stamp(base-timedelta(minutes=2))),
            ("NT-017", "Kavya Rao", "In-app", "Your pickup is confirmed for 3:30 PM at Koramangala hub.", "BK-006", "DELIVERED (SIMULATED)", stamp(base-timedelta(hours=1))),
            ("NT-016", "Ishaan Mehta", "In-app", "We are checking vehicle readiness before your pickup.", "BK-008", "QUEUED", stamp(base-timedelta(hours=2))),
            ("NT-015", "Mira Nair", "In-app", "Your booking details are confirmed. We look forward to seeing you.", "BK-011", "DELIVERED (SIMULATED)", stamp(base-timedelta(hours=4))),
        ]
        db.executemany("INSERT INTO notifications VALUES(?,?,?,?,?,?,?)", notifications)
        for row in vehicles:
            db.execute("INSERT INTO telemetry VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), row[0], row[9], row[6], row[12], row[14]))
        db.execute("INSERT INTO maintenance VALUES(?,?,?,?,?)", ("MT-001", "VH-004", "Brake system inspection", "OPEN", stamp(base+timedelta(days=1))))
        maintenance_reasons = {"VH-001": "Scheduled battery system inspection", "VH-008": "High-voltage system diagnostic", "VH-015": "Routine drivetrain service", "VH-022": "Scheduled safety inspection"}
        for vehicle_id, description in maintenance_reasons.items():
            db.execute("INSERT INTO maintenance VALUES(?,?,?,?,?)", (f"MT-{vehicle_id}", vehicle_id, description, "BLOCKING", stamp(base+timedelta(days=1))))
        for event, typ, ident, details in [
            ("Monitoring cycle completed", "system", "monitor-001", {"rules_checked": 8, "incidents_open": 3}),
            ("Safety policy blocked assignment", "vehicle", "VH-016", {"rule": "unsafe_vehicle_block", "result": "blocked"}),
            ("Replacement candidates ranked", "booking", "BK-003", {"eligible_count": 3, "algorithm": "hard filters then weighted score"}),
        ]:
            record_audit(db, event, typ, ident, details)


def rows(db: sqlite3.Connection, table: str, order: str = "") -> list[dict]:
    query = f"SELECT * FROM {table}" + (f" ORDER BY {order}" if order else "")
    return [dict(row) for row in db.execute(query)]


def public_incident(row: dict) -> dict:
    row["evidence"] = json.loads(row["evidence"])
    return row


def vehicle_state_reason(vehicle: dict, bookings: list[dict], incidents: list[dict], maintenance: list[dict]) -> str:
    active_incidents = [item for item in incidents if item["vehicle_id"] == vehicle["id"] and item["status"] not in ("RESOLVED", "REJECTED")]
    if vehicle["status"] == "UNSAFE" or vehicle["safety"] != "SAFE":
        if active_incidents:
            return f"Dispatch blocked: {active_incidents[0]['summary']}"
        if vehicle.get("notes"):
            return f"Dispatch blocked: {vehicle['notes']} Manager inspection is required before release."
        return f"Dispatch blocked because the safety state is {vehicle['safety']}. A manager must clear the fault before use."
    if vehicle["status"] == "MAINTENANCE":
        service = next((item for item in maintenance if item["vehicle_id"] == vehicle["id"] and item["status"] in ("OPEN", "BLOCKING")), None)
        if service:
            return f"Unavailable for service: {service['description']} ({service['status'].lower()})."
        return "Unavailable because the vehicle has a blocking maintenance status; no service note has been recorded yet."
    if vehicle["status"] == "BOOKED":
        booking = next((item for item in bookings if item["id"] == vehicle["current_booking"]), None)
        if booking:
            return f"Reserved for {booking['id']} ({booking['status'].lower()}); pickup at {booking['pickup_location']} on {booking['pickup_at']}."
        return "Marked booked, but no linked booking record was found. Review the vehicle assignment."
    if vehicle["status"] == "CHARGING":
        return f"Unavailable while {vehicle['energy_type'].lower()} energy is being replenished; current level is {vehicle['energy']}%."
    if vehicle["status"] == "AVAILABLE":
        freshness = now() - datetime.fromisoformat(vehicle["telemetry_at"])
        fresh_text = "Telemetry is current." if freshness.total_seconds() <= 30 * 60 else "Telemetry is stale; verify the vehicle before dispatch."
        return f"Available because it has no current booking, safety is clear, and service is not blocking. Energy is {vehicle['energy']}%; {fresh_text}"
    return f"Operating status is {vehicle['status']}; no more specific reason has been recorded."


def state() -> dict:
    with connect() as db:
        vehicles = rows(db, "vehicles", "id")
        bookings = rows(db, "bookings", "pickup_at")
        incidents = [public_incident(x) for x in rows(db, "incidents", "detected_at DESC")]
        approvals = rows(db, "approvals", "requested_at DESC")
        notifications = rows(db, "notifications", "created_at DESC")
        audit = rows(db, "audit_events", "created_at DESC")[:40]
        maintenance_records = rows(db, "maintenance", "scheduled_at DESC")
        for vehicle in vehicles:
            vehicle["state_reason"] = vehicle_state_reason(vehicle, bookings, incidents, maintenance_records)
        for event in audit:
            event["details"] = json.loads(event["details"])
        open_incidents = [item for item in incidents if item["status"] not in ("RESOLVED", "REJECTED")]
        active_bookings = [item for item in bookings if item["status"] in ("ACTIVE", "OVERDUE")]
        upcoming = [item for item in bookings if item["status"] == "CONFIRMED"]
        counts = {status: sum(1 for v in vehicles if v["status"] == status) for status in ("AVAILABLE", "BOOKED", "MAINTENANCE", "UNSAFE", "CHARGING")}
        available_pct = round(counts["AVAILABLE"] / max(1, len(vehicles)) * 100)
        safety_pct = round(sum(v["safety"] == "SAFE" for v in vehicles) / max(1, len(vehicles)) * 100)
        fulfilment_pct = max(0, 100 - 12 * sum(1 for i in open_incidents if i["severity"] in ("HIGH", "CRITICAL")))
        resilience = round(available_pct * .4 + safety_pct * .3 + fulfilment_pct * .3)
        return {"vehicles": vehicles, "bookings": bookings, "incidents": incidents, "approvals": approvals,
            "notifications": notifications, "maintenance": maintenance_records, "audit": audit, "analytics": {
                    "total_vehicles": len(vehicles), "available": counts["AVAILABLE"], "booked": counts["BOOKED"],
                    "maintenance": counts["MAINTENANCE"], "unsafe": counts["UNSAFE"], "active_bookings": len(active_bookings),
                    "upcoming_bookings": len(upcoming), "overdue_returns": sum(b["status"] == "OVERDUE" for b in bookings),
                    "high_priority": sum(i["severity"] in ("HIGH", "CRITICAL") for i in open_incidents),
                    "at_risk": sum(i["status"] not in ("RESOLVED", "REJECTED") and i["booking_id"] is not None for i in incidents),
                    "resilience": resilience, "resilience_factors": {"availability": available_pct, "safety": safety_pct, "fulfilment": fulfilment_pct},
                    "open_incidents": len(open_incidents), "automation_mode": SIMULATION["mode"], "simulation_running": SIMULATION["running"]}}


def candidates(db: sqlite3.Connection, booking: sqlite3.Row) -> list[dict]:
    all_vehicles = db.execute("SELECT * FROM vehicles WHERE id != ?", (booking["vehicle_id"],)).fetchall()
    ranked = []
    start, end = booking["pickup_at"], booking["return_at"]
    for vehicle in all_vehicles:
        reasons = []
        if vehicle["safety"] != "SAFE" or vehicle["status"] == "UNSAFE": reasons.append("safety fault or unsafe")
        if vehicle["service_status"] == "BLOCKING" or vehicle["status"] == "MAINTENANCE": reasons.append("blocking maintenance")
        if vehicle["category"] != booking["category"]: reasons.append("category mismatch")
        if vehicle["capacity"] < booking["capacity"]: reasons.append("insufficient passenger capacity")
        if vehicle["energy"] < booking["energy_required"]: reasons.append("insufficient energy")
        overlap = db.execute("SELECT 1 FROM bookings WHERE vehicle_id=? AND status IN ('ACTIVE','OVERDUE','CONFIRMED') AND pickup_at < ? AND return_at > ? LIMIT 1", (vehicle["id"], end, start)).fetchone()
        if overlap: reasons.append("booking overlap")
        if reasons: continue
        pickup = booking["pickup_location"].split()[0].lower()
        pickup_aliases = {"indiranagar": "Bengaluru", "koramangala": "Bengaluru", "whitefield": "Bengaluru", "powai": "Mumbai", "bandra": "Mumbai", "adyar": "Chennai", "hitech": "Hyderabad"}
        pickup_city = pickup_aliases.get(pickup, pickup.title())
        coordinates = {"Bengaluru": (12.9716, 77.5946), "Chennai": (13.0827, 80.2707), "Mumbai": (19.0760, 72.8777), "Hyderabad": (17.3850, 78.4867)}
        target_lat, target_lon = coordinates.get(pickup_city, (vehicle["latitude"], vehicle["longitude"]))
        lat1, lat2 = math.radians(target_lat), math.radians(vehicle["latitude"])
        delta_lat = lat2 - lat1
        delta_lon = math.radians(vehicle["longitude"] - target_lon)
        arc = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
        distance = 6371 * 2 * math.atan2(math.sqrt(arc), math.sqrt(1 - arc))
        if distance > 100: continue
        energy_score = min(100, vehicle["energy"])
        proximity_score = max(0, 100 - distance * 8)
        disruption = 100 if vehicle["location"] == pickup_city else 78
        score = round(proximity_score * .35 + energy_score * .25 + disruption * .25 + 85 * .15)
        ranked.append({"vehicle_id": vehicle["id"], "make": vehicle["make"], "model": vehicle["model"], "location": vehicle["location"], "energy": vehicle["energy"], "score": score, "distance_km": round(distance, 1), "breakdown": {"pickup_proximity": round(proximity_score), "energy_sufficiency": energy_score, "customer_disruption": disruption, "schedule_slack": 85}, "reason": "Eligible: safety clear, compatible class, sufficient energy, no overlapping booking."})
    return sorted(ranked, key=lambda item: item["score"], reverse=True)


def monitor() -> list[dict]:
    created = []
    with LOCK, connect() as db:
        vehicles = db.execute("SELECT * FROM vehicles").fetchall()
        active_bookings = db.execute("SELECT * FROM bookings WHERE status IN ('ACTIVE','OVERDUE','CONFIRMED')").fetchall()
        checks = []
        for booking in active_bookings:
            if not booking["vehicle_id"]:
                continue
            vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (booking["vehicle_id"],)).fetchone()
            booking_status = booking["status"]
            if booking_status in ("ACTIVE", "CONFIRMED") and datetime.fromisoformat(booking["return_at"]) < now():
                booking_status = "OVERDUE"
                db.execute("UPDATE bookings SET status='OVERDUE' WHERE id=?", (booking["id"],))
                record_audit(db, "Booking return became overdue", "booking", booking["id"], {"vehicle_id": booking["vehicle_id"], "return_at": booking["return_at"]})
            if booking_status == "OVERDUE":
                checks.append(("Overdue return", "HIGH", vehicle["id"], booking["id"], f"{booking['id']} passed its expected return time.", [f"Customer: {booking['customer']}", f"Return window: {booking['return_at']}", f"Vehicle: {vehicle['id']}"], "Check customer ETA and protect the next booking."))
            if vehicle["energy"] < booking["energy_required"]:
                checks.append(("Low battery", "MEDIUM", vehicle["id"], booking["id"], f"{vehicle['energy']}% energy is below {booking['energy_required']}% required for {booking['id']}.", [f"Current energy: {vehicle['energy']}%", f"Required: {booking['energy_required']}%", f"Pickup: {booking['pickup_at']}"], "Find a compatible replacement or charge before dispatch."))
            if vehicle["safety"] != "SAFE":
                checks.append(("Safety fault", "CRITICAL", vehicle["id"], booking["id"], f"{vehicle['id']} has an active safety fault.", ["Safety status is not clear", "Assignments blocked by policy"], "Block dispatch and escalate to the manager."))
        for vehicle in vehicles:
            if vehicle["safety"] != "SAFE" and not any(c[2] == vehicle["id"] and c[0] == "Safety fault" for c in checks):
                checks.append(("Safety fault", "CRITICAL", vehicle["id"], None, f"{vehicle['id']} has an active safety fault.", ["Safety status is not clear", "Assignments blocked by policy"], "Keep the vehicle out of service and escalate."))
        for vehicle in vehicles:
            freshness = now() - datetime.fromisoformat(vehicle["telemetry_at"])
            if freshness.total_seconds() > 30 * 60:
                checks.append(("Telemetry unavailable", "MEDIUM", vehicle["id"], None, f"Telemetry for {vehicle['id']} is stale.", [f"Last reading: {vehicle['telemetry_at']}", f"Age: {int(freshness.total_seconds() // 60)} minutes"], "Verify the device feed before dispatch."))
        for typ, severity, vehicle_id, booking_id, summary, evidence, recommendation in checks:
            exists = db.execute("SELECT 1 FROM incidents WHERE type=? AND vehicle_id IS ? AND booking_id IS ? AND status NOT IN ('RESOLVED','REJECTED')", (typ, vehicle_id, booking_id)).fetchone()
            if exists: continue
            incident_id = f"INC-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)", (incident_id, typ, severity, "DETECTED", vehicle_id, booking_id, summary, json.dumps(evidence), recommendation, stamp(), None))
            db.execute("INSERT INTO agent_recommendations VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), incident_id, recommendation, json.dumps(evidence), json.dumps([]), stamp()))
            record_audit(db, "Incident detected", "incident", incident_id, {"type": typ, "severity": severity, "vehicle_id": vehicle_id, "booking_id": booking_id})
            created.append({"id": incident_id, "type": typ, "summary": summary})
        record_audit(db, "Monitoring cycle completed", "system", "monitor", {"checks": len(checks), "new_incidents": len(created)})
    return created


def response(handler: BaseHTTPRequestHandler, status: int, data: dict | list | None = None, content_type: str = "application/json") -> None:
    body = (json.dumps(data, default=str) if content_type == "application/json" else str(data)).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


class SmartFleetHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def body(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self) -> None:
        response(self, 204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            return response(self, 200, state())
        if path == "/api/health":
            return response(self, 200, {"ok": True, "database": str(DB_PATH.name)})
        if path == "/api/telemetry":
            vehicle_id = parse_qs(urlparse(self.path).query).get("vehicle_id", [""])[0]
            with connect() as db:
                if not db.execute("SELECT 1 FROM vehicles WHERE id=?", (vehicle_id,)).fetchone():
                    return response(self, 404, {"error": "Vehicle not found"})
                readings = [dict(row) for row in db.execute("SELECT energy,location,safety,created_at FROM telemetry WHERE vehicle_id=? ORDER BY created_at DESC LIMIT 50", (vehicle_id,))]
            return response(self, 200, {"vehicle_id": vehicle_id, "readings": readings})
        if path == "/api/candidates":
            booking_id = urlparse(self.path).query.replace("booking_id=", "")
            with connect() as db:
                booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
                if not booking: return response(self, 404, {"error": "Booking not found"})
                return response(self, 200, {"candidates": candidates(db, booking)})
        if path.startswith("/api/"):
            key = path.removeprefix("/api/")
            mapping = {"vehicles": "vehicles", "bookings": "bookings", "incidents": "incidents", "approvals": "approvals", "notifications": "notifications", "audit": "audit_events"}
            if key in mapping:
                with connect() as db: return response(self, 200, rows(db, mapping[key], "created_at DESC" if key in ("notifications", "audit") else "id"))
            return response(self, 404, {"error": "API route not found"})
        if path == "/" or path == "/index.html":
            file = STATIC / "index.html"
        else:
            file = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() not in file.parents: return response(self, 403, "Forbidden", "text/plain")
        if not file.is_file(): return response(self, 404, "Not found", "text/plain")
        content_type = {".html": "text/html", ".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml"}.get(file.suffix, "application/octet-stream")
        return response(self, 200, file.read_text(encoding="utf-8"), content_type)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.body()
        except (ValueError, UnicodeDecodeError):
            return response(self, 400, {"error": "Request body must be valid JSON"})
        if path == "/api/monitor":
            return response(self, 200, {"created": monitor()})
        if path == "/api/incidents/decision":
            return self.decide_incident(payload)
        if path == "/api/settings/mode":
            mode = payload.get("mode")
            if mode not in ("Observe only", "Recommend actions", "Approval required", "Automation ON"):
                return response(self, 400, {"error": "Invalid automation mode"})
            SIMULATION["mode"] = mode
            return response(self, 200, {"mode": mode})
        if path == "/api/simulation/toggle":
            SIMULATION["running"] = bool(payload.get("running"))
            SIMULATION_WAKE.set()
            record_simple_audit("Simulation " + ("started" if SIMULATION["running"] else "paused"), "simulation", "main", {"running": SIMULATION["running"]})
            return response(self, 200, {"running": SIMULATION["running"]})
        if path == "/api/telemetry/override":
            return self.telemetry_override(payload)
        if path == "/api/simulation/scenario":
            return self.scenario(payload)
        if path == "/api/whatif":
            return self.whatif(payload)
        if path == "/api/reassign":
            return self.reassign(payload)
        if path == "/api/approvals/decision":
            return self.decide(payload)
        if path == "/api/bookings":
            return self.create_booking(payload)
        if path == "/api/notifications":
            with LOCK, connect() as db:
                booking = db.execute("SELECT customer FROM bookings WHERE id=?", (payload.get("booking_id"),)).fetchone()
                if not booking: return response(self, 404, {"error": "Related booking not found"})
                notification_id = f"NT-{int(time.time())}"
                db.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?)", (notification_id, booking["customer"], "In-app", str(payload.get("message", ""))[:500], payload["booking_id"], "QUEUED", stamp()))
                record_audit(db, "Simulated notification queued", "notification", notification_id, {"booking_id": payload["booking_id"], "delivery": "not sent externally"})
            return response(self, 201, {"id": notification_id, "status": "QUEUED"})
        return response(self, 404, {"error": "API route not found"})

    def decide_incident(self, payload: dict) -> None:
        incident_id = str(payload.get("incident_id", ""))
        decision = payload.get("decision")
        if decision not in ("approve", "reject"):
            return response(self, 400, {"error": "Decision must be approve or reject"})
        reason = str(payload.get("reason", "")).strip()[:500]
        if decision == "reject" and not reason:
            return response(self, 400, {"error": "A reason is required to reject an incident recommendation"})
        with LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            incident = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                return response(self, 404, {"error": "Incident not found"})
            if incident["status"] in ("RESOLVED", "REJECTED"):
                return response(self, 409, {"error": "This incident is already closed"})
            if decision == "approve":
                status = "ACTION READY"
                db.execute("UPDATE incidents SET status=? WHERE id=?", (status, incident_id))
                record_audit(db, "Incident recommendation approved", "incident", incident_id, {"manager": payload.get("manager", "Arjun Singh"), "next_step": "Action is ready; restricted operations still require their own approval."})
            else:
                status = "REJECTED"
                db.execute("UPDATE incidents SET status=?,resolved_at=? WHERE id=?", (status, stamp(), incident_id))
                pending = db.execute("SELECT id FROM approvals WHERE incident_id=? AND status='PENDING'", (incident_id,)).fetchall()
                for approval in pending:
                    db.execute("UPDATE approvals SET status='REJECTED',decided_at=?,decided_by=?,decision_reason=? WHERE id=?", (stamp(), payload.get("manager", "Arjun Singh"), reason, approval["id"]))
                    record_audit(db, "Related approval rejected with incident", "approval", approval["id"], {"incident_id": incident_id, "reason": reason})
                record_audit(db, "Incident recommendation rejected", "incident", incident_id, {"manager": payload.get("manager", "Arjun Singh"), "reason": reason})
        return response(self, 200, {"incident_id": incident_id, "status": status})

    def telemetry_override(self, payload: dict) -> None:
        vehicle_id = payload.get("vehicle_id")
        energy, location, safety = payload.get("energy"), payload.get("location"), payload.get("safety")
        if energy is not None and (not isinstance(energy, int) or not 0 <= energy <= 100):
            return response(self, 400, {"error": "Energy must be an integer from 0 to 100"})
        if safety is not None and safety not in ("SAFE", "FAULT"):
            return response(self, 400, {"error": "Safety must be SAFE or FAULT"})
        if location is not None and (not isinstance(location, str) or not location.strip() or len(location) > 100):
            return response(self, 400, {"error": "Location must contain 1 to 100 characters"})
        with LOCK, connect() as db:
            vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle: return response(self, 404, {"error": "Vehicle not found"})
            next_energy = vehicle["energy"] if energy is None else energy
            next_location = vehicle["location"] if location is None else location.strip()
            next_safety = vehicle["safety"] if safety is None else safety
            next_status = "UNSAFE" if next_safety != "SAFE" else vehicle["status"]
            event_time = stamp()
            db.execute("UPDATE vehicles SET energy=?,location=?,safety=?,status=?,telemetry_at=? WHERE id=?", (next_energy, next_location, next_safety, next_status, event_time, vehicle_id))
            db.execute("INSERT INTO telemetry VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), vehicle_id, next_energy, next_location, next_safety, event_time))
            record_audit(db, "Manual telemetry override", "vehicle", vehicle_id, {"energy": next_energy, "location": next_location, "safety": next_safety})
        if next_safety != "SAFE": monitor()
        return response(self, 200, {"vehicle_id": vehicle_id, "energy": next_energy, "location": next_location, "safety": next_safety, "status": next_status})

    def scenario(self, payload: dict) -> None:
        scenario = payload.get("scenario")
        allowed = {"Vehicle breakdown", "Overdue return", "Booking surge", "Low battery", "Safety fault", "Damage dispute", "Telemetry loss", "Immobilisation request"}
        if scenario not in allowed: return response(self, 400, {"error": "Unknown demo scenario"})
        if payload.get("apply"):
            vehicle_id = payload.get("vehicle_id", "VH-003")
            with LOCK, connect() as db:
                vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
                if not vehicle: return response(self, 404, {"error": "Vehicle not found"})
                if scenario in ("Vehicle breakdown", "Safety fault"):
                    db.execute("UPDATE vehicles SET status='UNSAFE', safety='FAULT', notes=? WHERE id=?", (scenario, vehicle_id))
                elif scenario == "Low battery":
                    db.execute("UPDATE vehicles SET energy=12 WHERE id=?", (vehicle_id,))
                elif scenario == "Telemetry loss":
                    db.execute("UPDATE vehicles SET telemetry_at=? WHERE id=?", (stamp(now()-timedelta(hours=3)), vehicle_id))
                elif scenario == "Overdue return":
                    changed = db.execute("UPDATE bookings SET status='OVERDUE', return_at=? WHERE vehicle_id=? AND status IN ('ACTIVE','CONFIRMED')", (stamp(now()-timedelta(minutes=42)), vehicle_id)).rowcount
                    if not changed:
                        booking_id = f"BK-SIM-{uuid.uuid4().hex[:8]}"
                        db.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (booking_id, "Scenario Customer", "", vehicle_id, vehicle["category"], 4, f"{vehicle['location']} hub", stamp(now()-timedelta(hours=3)), stamp(now()-timedelta(minutes=42)), "OVERDUE", 30, stamp()))
                        db.execute("UPDATE vehicles SET current_booking=? WHERE id=?", (booking_id, vehicle_id))
                        record_audit(db, "Scenario overdue booking created", "booking", booking_id, {"vehicle_id": vehicle_id})
                elif scenario == "Booking surge":
                    for _ in range(3):
                        booking_id = f"BK-SIM-{uuid.uuid4().hex[:8]}"
                        pickup_at = now() + timedelta(hours=1)
                        db.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (booking_id, "Scenario Customer", "", None, "Compact", 4, "Bengaluru hub", stamp(pickup_at), stamp(pickup_at+timedelta(hours=4)), "CONFIRMED", 30, stamp()))
                        record_audit(db, "Booking surge request created", "booking", booking_id, {"scenario": scenario, "vehicle_assigned": False})
                elif scenario == "Damage dispute":
                    incident_id = f"INC-{int(time.time())}"
                    incident_id = f"INC-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                    approval_id = f"AP-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                    db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)", (incident_id, scenario, "HIGH", "AWAITING APPROVAL", vehicle_id, vehicle["current_booking"], f"Damage dispute reported for {vehicle_id}; liability not determined.", json.dumps(["Reported damage requires manager review", "No liability conclusion has been made"]), "Preserve evidence and request manager review.", stamp(), None))
                    db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (approval_id, incident_id, "Damage dispute review", "Manager review required; the agent has made no liability decision.", vehicle_id, vehicle["current_booking"], vehicle_id, "PENDING", stamp(), None, None, None, json.dumps({"vehicle_id": vehicle_id, "vehicle_status": vehicle["status"], "safety": vehicle["safety"]})))
                    record_audit(db, "Damage dispute escalated", "incident", incident_id, {"approval_id": approval_id, "liability": "undetermined"})
                elif scenario == "Immobilisation request":
                    reason = str(payload.get("reason", "")).strip()
                    if not reason: return response(self, 400, {"error": "A documented reason is required"})
                    incident_id = f"INC-{int(time.time())}"
                    incident_id = f"INC-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                    approval_id = f"AP-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                    db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)", (incident_id, scenario, "HIGH", "AWAITING APPROVAL", vehicle_id, vehicle["current_booking"], f"Simulated immobilisation requested for {vehicle_id}.", json.dumps([f"Manager reason: {reason}", "No physical vehicle control is connected"]), "Manager approval required. This request changes demo state only.", stamp(), None))
                    db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (approval_id, incident_id, "Simulated immobilisation", reason, vehicle_id, vehicle["current_booking"], vehicle_id, "PENDING", stamp(), None, None, None, json.dumps({"vehicle_id": vehicle_id, "vehicle_status": vehicle["status"], "safety": vehicle["safety"]})))
                    record_audit(db, "Simulated immobilisation approval requested", "incident", incident_id, {"approval_id": approval_id, "physical_action": False})
                record_audit(db, "Scenario applied to live demo data", "vehicle", vehicle_id, {"scenario": scenario})
            monitor()
            return response(self, 200, {"applied": True, "scenario": scenario})
        return response(self, 200, self.scenario_projection(scenario))

    def scenario_projection(self, scenario: str) -> dict:
        data = state()
        affected = [b for b in data["bookings"] if b["status"] in ("CONFIRMED", "ACTIVE", "OVERDUE")][:3]
        with connect() as db:
            options = candidates(db, db.execute("SELECT * FROM bookings WHERE id=?", (affected[0]["id"],)).fetchone()) if affected else []
        return {"scenario": scenario, "bookings_affected": len(affected), "booking_refs": [b["id"] for b in affected],
                "eligible_replacements": len(options), "top_options": options[:3], "unresolved_conflicts": max(0, len(affected)-len(options)),
                "estimated_delay_minutes": 18 if options else 75, "estimated_cost_impact": DEMO_COST_WITH_REPLACEMENT if options else DEMO_COST_WITHOUT_REPLACEMENT,
            "approval_required": scenario in ("Safety fault", "Damage dispute", "Vehicle breakdown", "Immobilisation request"),
                "outcome": "Projected only; live records are unchanged until Apply scenario is selected."}

    def whatif(self, payload: dict) -> None:
        scenario = payload.get("scenario", "Vehicle breakdown")
        if scenario not in {"Vehicle breakdown", "Overdue return", "Booking surge", "Low battery", "Safety fault", "Damage dispute", "Telemetry loss", "Immobilisation request"}:
            return response(self, 400, {"error": "Unknown scenario"})
        return response(self, 200, self.scenario_projection(scenario))

    def reassign(self, payload: dict) -> None:
        booking_id, vehicle_id = payload.get("booking_id"), payload.get("vehicle_id")
        with LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not booking or not vehicle: return response(self, 404, {"error": "Booking or vehicle not found"})
            eligible = candidates(db, booking)
            candidate = next((item for item in eligible if item["vehicle_id"] == vehicle_id), None)
            if not candidate: return response(self, 409, {"error": "Vehicle fails one or more safety, maintenance, compatibility, energy or schedule checks."})
            incident = db.execute("SELECT severity FROM incidents WHERE id=?", (payload.get("incident_id"),)).fetchone()
            if SIMULATION["mode"] == "Observe only":
                return response(self, 409, {"error": "Observe-only mode blocks booking changes"})
            approval_required = bool(payload.get("approval_required")) or SIMULATION["mode"] != "Automation ON" or bool(incident and incident["severity"] in ("HIGH", "CRITICAL"))
            if approval_required:
                existing = db.execute("SELECT id FROM approvals WHERE booking_id=? AND proposed_vehicle_id=? AND status='PENDING' LIMIT 1", (booking_id, vehicle_id)).fetchone()
                if existing:
                    return response(self, 200, {"approval_id": existing["id"], "status": "PENDING", "candidate": candidate})
                approval_id = f"AP-{int(time.time())}-{uuid.uuid4().hex[:6]}"
                snapshot = {"booking_vehicle": booking["vehicle_id"], "candidate_energy": vehicle["energy"], "candidate_status": vehicle["status"], "candidate_safety": vehicle["safety"], "candidate_service": vehicle["service_status"], "return_at": booking["return_at"]}
                db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (approval_id, payload.get("incident_id", "INC-1042"), "Reassign booking", "Manager review requested for customer-impacting change.", booking["vehicle_id"], booking_id, vehicle_id, "PENDING", stamp(), None, None, None, json.dumps(snapshot)))
                record_audit(db, "Approval requested", "approval", approval_id, snapshot)
                return response(self, 201, {"approval_id": approval_id, "status": "PENDING", "candidate": candidate})
            return self.apply_reassignment(db, booking, vehicle, candidate)

    def apply_reassignment(self, db: sqlite3.Connection, booking: sqlite3.Row, vehicle: sqlite3.Row, candidate: dict) -> None:
        old_vehicle_id = booking["vehicle_id"]
        db.execute("UPDATE bookings SET vehicle_id=? WHERE id=?", (vehicle["id"], booking["id"]))
        db.execute("UPDATE vehicles SET current_booking=NULL, status='AVAILABLE' WHERE id=?", (old_vehicle_id,))
        db.execute("UPDATE vehicles SET current_booking=?, status='BOOKED' WHERE id=?", (booking["id"], vehicle["id"]))
        db.execute("UPDATE incidents SET status='RESOLVED', resolved_at=? WHERE booking_id=? AND status NOT IN ('RESOLVED','REJECTED')", (stamp(), booking["id"]))
        message = f"Your booking vehicle has been updated to {vehicle['make']} {vehicle['model']} ({vehicle['id']}). Pickup remains at {booking['pickup_location']}."
        notice_id = f"NT-{int(time.time())}"
        db.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?)", (notice_id, booking["customer"], "In-app", message, booking["id"], "QUEUED", stamp()))
        record_audit(db, "Booking reassigned", "booking", booking["id"], {"from": old_vehicle_id, "to": vehicle["id"], "score": candidate["score"]})
        record_audit(db, "Simulated customer notification queued", "notification", notice_id, {"booking_id": booking["id"], "delivery": "queued; not sent externally"})
        return response(self, 200, {"booking_id": booking["id"], "vehicle_id": vehicle["id"], "notification_id": notice_id, "status": "REASSIGNED"})

    def decide(self, payload: dict) -> None:
        approval_id, decision = payload.get("approval_id"), payload.get("decision")
        if decision not in ("approve", "reject", "modify"): return response(self, 400, {"error": "Decision must be approve, reject or modify"})
        with LOCK, connect() as db:
            db.execute("BEGIN IMMEDIATE")
            approval = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not approval: return response(self, 404, {"error": "Approval not found"})
            if approval["status"] != "PENDING": return response(self, 409, {"error": "Approval has already been decided"})
            if decision == "modify":
                if approval["operation"] != "Reassign booking": return response(self, 409, {"error": "This approval does not support changing a replacement vehicle"})
                booking = db.execute("SELECT * FROM bookings WHERE id=?", (approval["booking_id"],)).fetchone()
                current = json.loads(approval["snapshot"])
                if not booking or booking["vehicle_id"] != current["booking_vehicle"] or booking["return_at"] != current["return_at"]:
                    return response(self, 409, {"error": "Approval is stale because the booking changed"})
                vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (payload.get("vehicle_id"),)).fetchone()
                candidate = next((item for item in candidates(db, booking) if vehicle and item["vehicle_id"] == vehicle["id"]), None)
                if not candidate: return response(self, 409, {"error": "Modified vehicle fails current policy checks"})
                snapshot = {"booking_vehicle": booking["vehicle_id"], "candidate_energy": vehicle["energy"], "candidate_status": vehicle["status"], "candidate_safety": vehicle["safety"], "candidate_service": vehicle["service_status"], "return_at": booking["return_at"]}
                db.execute("UPDATE approvals SET proposed_vehicle_id=?, snapshot=?, reason=? WHERE id=?", (vehicle["id"], json.dumps(snapshot), payload.get("reason", "Manager modified proposed vehicle"), approval_id))
                record_audit(db, "Approval recommendation modified", "approval", approval_id, {"proposed_vehicle_id": vehicle["id"], "score": candidate["score"]})
                return response(self, 200, {"status": "PENDING", "proposed_vehicle_id": vehicle["id"]})
            if decision == "reject":
                db.execute("UPDATE approvals SET status='REJECTED', decided_at=?, decided_by=?, decision_reason=? WHERE id=?", (stamp(), payload.get("manager", "Arjun Singh"), payload.get("reason", "Rejected by manager"), approval_id))
                db.execute("UPDATE incidents SET status='REJECTED', resolved_at=? WHERE id=? AND status NOT IN ('RESOLVED','REJECTED')", (stamp(), approval["incident_id"]))
                record_audit(db, "Approval rejected", "approval", approval_id, {"reason": payload.get("reason", "Rejected by manager")})
                record_audit(db, "Incident recommendation rejected", "incident", approval["incident_id"], {"approval_id": approval_id, "reason": payload.get("reason", "Rejected by manager")})
                return response(self, 200, {"status": "REJECTED"})
            if approval["operation"] in ("Damage dispute review", "Simulated immobilisation"):
                snapshot = json.loads(approval["snapshot"])
                vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (approval["vehicle_id"],)).fetchone()
                if not vehicle or vehicle["status"] != snapshot["vehicle_status"] or vehicle["safety"] != snapshot["safety"]:
                    db.execute("UPDATE approvals SET status='STALE', decided_at=?, decided_by=?, decision_reason=? WHERE id=?", (stamp(), payload.get("manager", "Arjun Singh"), "Vehicle state changed since the request.", approval_id))
                    record_audit(db, "Stale approval rejected", "approval", approval_id, {"reason": "Vehicle state changed since request"})
                    return response(self, 409, {"error": "Approval is stale because the vehicle changed. Run the agent again."})
                if approval["operation"] == "Simulated immobilisation":
                    db.execute("UPDATE vehicles SET status='UNSAFE', safety='FAULT', notes='Simulated immobilisation approved; no physical control.' WHERE id=?", (vehicle["id"],))
                    record_audit(db, "Simulated immobilisation approved", "vehicle", vehicle["id"], {"physical_action": False, "manager": payload.get("manager", "Arjun Singh")})
                else:
                    db.execute("UPDATE incidents SET status='INVESTIGATING' WHERE id=?", (approval["incident_id"],))
                    record_audit(db, "Damage dispute assigned for manager handling", "incident", approval["incident_id"], {"liability": "undetermined", "manager": payload.get("manager", "Arjun Singh")})
                db.execute("UPDATE approvals SET status='APPROVED', decided_at=?, decided_by=? WHERE id=?", (stamp(), payload.get("manager", "Arjun Singh"), approval_id))
                return response(self, 200, {"status": "APPROVED", "operation": approval["operation"], "physical_action": False})
            booking = db.execute("SELECT * FROM bookings WHERE id=?", (approval["booking_id"],)).fetchone()
            vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (approval["proposed_vehicle_id"],)).fetchone()
            snapshot = json.loads(approval["snapshot"])
            if not booking or not vehicle or booking["vehicle_id"] != snapshot["booking_vehicle"] or booking["return_at"] != snapshot["return_at"] or vehicle["energy"] != snapshot["candidate_energy"] or vehicle["status"] != snapshot.get("candidate_status", vehicle["status"]) or vehicle["safety"] != snapshot.get("candidate_safety", vehicle["safety"]) or vehicle["service_status"] != snapshot.get("candidate_service", vehicle["service_status"]):
                db.execute("UPDATE approvals SET status='STALE', decided_at=?, decided_by=?, decision_reason=? WHERE id=?", (stamp(), payload.get("manager", "Arjun Singh"), "Underlying vehicle or booking changed since request.", approval_id))
                record_audit(db, "Stale approval rejected", "approval", approval_id, {"reason": "State changed since recommendation"})
                return response(self, 409, {"error": "Approval is stale because the vehicle or booking changed. Run the agent again."})
            candidate = next((item for item in candidates(db, booking) if item["vehicle_id"] == vehicle["id"]), None)
            if not candidate: return response(self, 409, {"error": "Current policy checks reject this assignment."})
            db.execute("UPDATE approvals SET status='APPROVED', decided_at=?, decided_by=? WHERE id=?", (stamp(), payload.get("manager", "Arjun Singh"), approval_id))
            result = self.apply_reassignment(db, booking, vehicle, candidate)
            record_audit(db, "Approval executed", "approval", approval_id, {"manager": payload.get("manager", "Arjun Singh")})
            return result

    def create_booking(self, payload: dict) -> None:
        required = ("customer", "category", "pickup_location", "pickup_at", "return_at")
        if any(not str(payload.get(field, "")).strip() for field in required): return response(self, 400, {"error": "Customer, category, pickup location and time window are required"})
        if payload["return_at"] <= payload["pickup_at"]: return response(self, 400, {"error": "Return time must be after pickup time"})
        booking_id = f"BK-{int(time.time())}"
        vehicle_id = payload.get("vehicle_id") or None
        with LOCK, connect() as db:
            if vehicle_id:
                vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
                if not vehicle: return response(self, 404, {"error": "Vehicle not found"})
                if vehicle["safety"] != "SAFE" or vehicle["status"] in ("UNSAFE", "MAINTENANCE"): return response(self, 409, {"error": "Unsafe or maintenance-blocked vehicles cannot be assigned"})
                if vehicle["category"] != payload["category"] or vehicle["capacity"] < int(payload.get("capacity", 4)):
                    return response(self, 409, {"error": "Vehicle does not match the requested class or passenger capacity"})
                if vehicle["energy"] < int(payload.get("energy_required", 30)):
                    return response(self, 409, {"error": "Vehicle energy is below the booking requirement"})
                overlap = db.execute("SELECT 1 FROM bookings WHERE vehicle_id=? AND status IN ('ACTIVE','OVERDUE','CONFIRMED') AND pickup_at < ? AND return_at > ?", (vehicle_id, payload["return_at"], payload["pickup_at"])).fetchone()
                if overlap: return response(self, 409, {"error": "Booking conflicts with an existing vehicle reservation"})
            db.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (booking_id, payload["customer"], payload.get("phone", ""), vehicle_id, payload["category"], int(payload.get("capacity", 4)), payload["pickup_location"], payload["pickup_at"], payload["return_at"], "CONFIRMED", int(payload.get("energy_required", 30)), stamp()))
            if vehicle_id:
                db.execute("UPDATE vehicles SET status='BOOKED', current_booking=? WHERE id=?", (booking_id, vehicle_id))
            record_audit(db, "Booking created", "booking", booking_id, {"vehicle_id": vehicle_id, "customer": payload["customer"]})
        return response(self, 201, {"id": booking_id, "status": "CONFIRMED"})


def record_simple_audit(event: str, entity_type: str, entity_id: str, details: dict) -> None:
    with LOCK, connect() as db: record_audit(db, event, entity_type, entity_id, details)


def simulate_tick() -> int:
    with LOCK, connect() as db:
        vehicles = db.execute("SELECT * FROM vehicles WHERE status NOT IN ('UNSAFE','MAINTENANCE')").fetchall()
        event_time = stamp()
        for vehicle in vehicles:
            energy = max(0, vehicle["energy"] - (1 if vehicle["status"] == "BOOKED" else 0))
            db.execute("UPDATE vehicles SET energy=?, telemetry_at=? WHERE id=?", (energy, event_time, vehicle["id"]))
            db.execute("INSERT INTO telemetry VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), vehicle["id"], energy, vehicle["location"], vehicle["safety"], event_time))
        record_audit(db, "Telemetry simulation tick", "simulation", "main", {"vehicles_updated": len(vehicles), "energy_drain": "1 point per booked vehicle"})
        return len(vehicles)


def simulation_loop() -> None:
    while True:
        if not SIMULATION["running"]:
            SIMULATION_WAKE.wait()
            SIMULATION_WAKE.clear()
            continue
        if SIMULATION_WAKE.wait(SIMULATION["interval"]):
            SIMULATION_WAKE.clear()
            continue
        simulate_tick()


def main() -> None:
    seed()
    threading.Thread(target=simulation_loop, daemon=True).start()
    host = os.environ.get("SMARTFLEET_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"SmartFleet running at http://{host}:{port} (database: {DB_PATH})")
    HTTPServer((host, port), SmartFleetHandler).serve_forever()


if __name__ == "__main__":
    main()
