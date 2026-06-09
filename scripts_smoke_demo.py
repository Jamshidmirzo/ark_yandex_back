"""Live end-to-end smoke test against a running dev server (http://127.0.0.1:8000).

Drives the full planned-scheduling flow over HTTP, exactly as the frontend will:
create A→B order with duration → submit → approve → shift → claim(window) →
overlapping claim returns 409 TIME_CONFLICT → start → location → schedule →
estimate. Prints a PASS/FAIL line per step.
"""

import datetime
import os
import sys

import requests

B = "http://127.0.0.1:8000/api/v1"
CT = int(os.environ["CT_ID"])
CAR = int(os.environ["CAR_ID"])

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + extra) if extra else ''}")


def login(u):
    r = requests.post(f"{B}/auth/login/", json={"username": u, "password": "pw"}, timeout=10)
    r.raise_for_status()
    return r.json()["access"]


def h(t):
    return {"Authorization": f"Bearer {t}"}


req, disp, drv = login("req"), login("disp"), login("drv")
print("logged in: req / disp / drv")

now = datetime.datetime.now(datetime.timezone.utc)
start = (now + datetime.timedelta(hours=1)).isoformat()


def make_order(start_iso, minutes):
    body = {
        "address": "Амир Темур 1",
        "car_type_id": CT,
        "planned_datetime": start_iso,
        "estimated_duration": minutes,
        "service_time": 30,
        "origin_lat": 41.311,
        "origin_lng": 69.240,
        "address_lat": 41.351,
        "address_lng": 69.290,
    }
    r = requests.post(f"{B}/car-orders/", json=body, headers=h(req), timeout=10)
    oid = r.json()["id"]
    requests.post(f"{B}/car-orders/{oid}/submit/", headers=h(req), timeout=10)
    requests.post(f"{B}/car-orders/{oid}/admin-approve/", headers=h(disp), timeout=10)
    return oid, r.json()


# 1. create A (5h) and check the minutes contract on the way back
a, adata = make_order(start, 300)
check("create order A returns estimated_duration in minutes", adata.get("estimated_duration") == 300,
      f"got {adata.get('estimated_duration')}")
check("planned_end derived", bool(adata.get("planned_end")))

# 2. driver goes on shift with the seeded car
rs = requests.patch(f"{B}/car-orders/drivers/me/shift/", json={"car_id": CAR}, headers=h(drv), timeout=10)
check("driver on shift", rs.status_code == 200, f"http {rs.status_code}")

# 3. claim reserves the window → scheduled
rc = requests.post(f"{B}/car-orders/{a}/claim/", headers=h(drv), timeout=10)
check("claim A → scheduled", rc.status_code == 200 and rc.json().get("status") == "scheduled",
      rc.json().get("status", rc.text[:80]))

# 4. an overlapping order is rejected with 409 TIME_CONFLICT
overlap_start = (now + datetime.timedelta(hours=3)).isoformat()  # inside A's 1h..6h window
b, _ = make_order(overlap_start, 120)
rco = requests.post(f"{B}/car-orders/{b}/claim/", headers=h(drv), timeout=10)
conflict = rco.json().get("error", {})
check("overlapping claim → 409 TIME_CONFLICT", rco.status_code == 409 and conflict.get("code") == "TIME_CONFLICT",
      f"http {rco.status_code} {conflict.get('code')}")
check("conflict details carry order_id", conflict.get("details", {}).get("order_id") == a,
      str(conflict.get("details")))

# 5. start A → in_progress (only one active)
rst = requests.post(f"{B}/car-orders/{a}/start/", headers=h(drv), timeout=10)
check("start A → in_progress", rst.status_code == 200 and rst.json().get("status") == "in_progress",
      rst.json().get("status", rst.text[:80]))

# 6. driver location heartbeat → author sees it
requests.post(f"{B}/car-orders/drivers/me/location/", json={"lat": 41.32, "lng": 69.25}, headers=h(drv), timeout=10)
det = requests.get(f"{B}/car-orders/{a}/", headers=h(req), timeout=10).json()
loc = det.get("driver_location")
check("author sees live driver_location", bool(loc) and abs(loc["lat"] - 41.32) < 1e-6, str(loc))

# 7. driver schedule lists the committed order
sched = requests.get(f"{B}/car-orders/drivers/me/schedule/", headers=h(drv), timeout=10).json()
sched_list = sched if isinstance(sched, list) else sched.get("results", [])
check("schedule lists A", any(o["id"] == a for o in sched_list), f"{len(sched_list)} item(s)")

# 8. estimate endpoint (OSRM or haversine fallback)
est = requests.post(f"{B}/car-orders/estimate/",
                    json={"origin_lat": 41.311, "origin_lng": 69.240, "dest_lat": 41.351, "dest_lng": 69.290},
                    headers=h(disp), timeout=15).json()
check("estimate returns minutes + geometry", est.get("duration_minutes", 0) > 0 and len(est.get("geometry", [])) >= 2,
      f"{est.get('duration_minutes')} min, {len(est.get('geometry', []))} pts, src={est.get('source')}")

print(f"\nORDER_A={a}  (use: manage.py simulate_driver --order {a})")
print("RESULT:", "ALL PASS ✅" if ok else "SOME FAILED ❌")
sys.exit(0 if ok else 1)
