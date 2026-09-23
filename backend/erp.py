"""NTAXCO ERP business modules: MongoDB-backed generic CRUD and workflows.

The legacy seed datasets remain in this source only as schema/reference material;
production startup never inserts them unless NTAXCO_ENABLE_DEMO_SEED=true."""
from fastapi import APIRouter, Body, HTTPException, Query, Depends
from datetime import datetime, timezone, timedelta
import uuid
import bcrypt
import os


def _hash_password(password: str) -> str:
    """Same bcrypt scheme as server.py's hash_password/verify_password so an
    employee password set here authenticates through the existing login path."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _validate_percentage(body: dict, field: str):
    """Shared 0-100 (decimals allowed) validation for agent/customer commission fields."""
    if field not in body or body[field] in (None, ""):
        return
    try:
        value = float(body[field])
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field.replace('_', ' ').title()} must be a number")
    if value < 0 or value > 100:
        raise HTTPException(status_code=422, detail=f"{field.replace('_', ' ').title()} must be between 0 and 100")
    body[field] = value

# India Standard Time offset. Attendance check-in/out is always evaluated
# and stamped using server-side IST time (never a client-supplied
# timestamp) so it can't be spoofed and stays consistent regardless of the
# employee's browser timezone/clock. Stored as simple "HH:MM" / "YYYY-MM-DD"
# strings to match the existing seeded attendance format.
IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + IST_OFFSET


def _ist_today_str() -> str:
    return _ist_now().strftime("%Y-%m-%d")


def _ist_time_str() -> str:
    return _ist_now().strftime("%H:%M")


def _hours_between(check_in: str, check_out: str) -> str:
    try:
        ih, im = (int(x) for x in check_in.split(":"))
        oh, om = (int(x) for x in check_out.split(":"))
        mins = (oh * 60 + om) - (ih * 60 + im)
        if mins < 0:
            mins = 0
        return f"{mins // 60}h {mins % 60:02d}m"
    except Exception:
        return "-"

READ_ROLES = {
    "employees": {"admin", "employee"}, "customers": {"admin", "employee", "agent", "customer"},
    "bookings": {"admin", "employee", "agent", "customer"}, "projects": {"admin", "employee", "agent", "customer"},
    "invoices": {"admin", "employee", "customer"}, "documents": {"admin", "employee", "agent", "customer"},
    "tickets": {"admin", "employee", "customer"}, "services": {"admin", "employee", "agent", "customer"},
    "gst": {"admin", "employee", "customer"}, "itr": {"admin", "employee", "customer"},
    "tds": {"admin", "employee", "customer"}, "roc": {"admin", "employee", "customer"},
    "attendance": {"admin", "employee"}, "leaves": {"admin", "employee"}, "tasks": {"admin", "employee"},
    "payslips": {"admin", "employee"}, "leads": {"admin", "agent"}, "appointments": {"admin", "employee", "agent"},
    "commissions": {"admin", "agent"}, "journal": {"admin"}, "payments": {"admin", "customer"},
    "agents": {"admin"}, "site-images": {"admin"},
}

WRITE_ROLES = {
    "employees": {"admin"}, "customers": {"admin", "agent"}, "bookings": {"admin", "customer", "agent"},
    "projects": {"admin"}, "invoices": {"admin"}, "documents": {"admin", "employee", "customer"},
    "tickets": {"admin", "customer"}, "services": {"admin"}, "gst": {"admin"}, "itr": {"admin"},
    "tds": {"admin"}, "roc": {"admin"}, "attendance": {"admin", "employee"}, "leaves": {"admin", "employee"},
    "tasks": {"admin", "employee"}, "payslips": {"admin"}, "leads": {"admin", "agent"},
    "appointments": {"admin", "agent"}, "commissions": {"admin"}, "journal": {"admin"},
    "payments": {"admin"}, "agents": {"admin"}, "site-images": {"admin"},
}

# ---------------- Centralized image management ----------------
# Placements an admin-uploaded image can be assigned to on the customer site.
SITE_IMAGE_PLACEMENTS = {"home", "dashboard", "projects", "services"}

# Roughly 5MB of raw image data once base64-encoded (base64 inflates size by
# ~4/3), used to keep a single Mongo document well under the 16MB doc limit
# and to stop an admin from accidentally uploading something huge.
MAX_IMAGE_DATA_LEN = 7_000_000


ALLOWED_DOCUMENT_MIMES = {"application/pdf", "image/jpeg", "image/png", "image/webp", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
MAX_DOCUMENT_DATA_LEN = 7_000_000

def _validate_document_upload(body: dict, user: dict) -> None:
    if "file_data" not in body:
        return
    data = str(body.get("file_data") or "")
    if len(data) > MAX_DOCUMENT_DATA_LEN:
        raise HTTPException(status_code=400, detail="Document is too large. Please upload a file under ~5MB.")
    if not data.startswith("data:") or ";base64," not in data[:120]:
        raise HTTPException(status_code=400, detail="Invalid document upload format")
    mime = data[5:data.find(";base64,")]
    if mime not in ALLOWED_DOCUMENT_MIMES:
        raise HTTPException(status_code=400, detail="Unsupported document type")
    body["mime_type"] = mime
    body["file_data"] = data
    body.setdefault("status", "Uploaded")
    body.setdefault("uploaded_by", user.get("name") or "Customer Upload")
    body.setdefault("uploaded_date", _ist_today_str())

def _validate_site_image(body: dict, *, partial: bool = False) -> None:
    """Normalize + validate a site-image payload in place. When `partial` is
    True (updates), a field is only checked if the caller actually sent it —
    so an edit that only changes the title doesn't need to resend the image."""
    if "placement" in body or not partial:
        placement = str(body.get("placement", "")).strip().lower()
        if placement not in SITE_IMAGE_PLACEMENTS:
            raise HTTPException(
                status_code=400,
                detail=f"Placement must be one of: {', '.join(sorted(SITE_IMAGE_PLACEMENTS))}",
            )
        body["placement"] = placement
    if "image" in body or not partial:
        image = str(body.get("image", "")).strip()
        if not image:
            raise HTTPException(status_code=400, detail="An image (file upload or URL) is required")
        if len(image) > MAX_IMAGE_DATA_LEN:
            raise HTTPException(status_code=400, detail="Image is too large. Please upload an image under ~5MB.")
        body["image"] = image
    if "title" in body:
        body["title"] = str(body.get("title") or "").strip()
    if "status" in body:
        status = str(body.get("status") or "Active").strip() or "Active"
        body["status"] = status

def _ok(data, message="OK", pagination=None):
    return {
        "success": True,
        "message": message,
        "data": data,
        "pagination": pagination,
        "meta": {"count": len(data) if isinstance(data, list) else 1},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": str(uuid.uuid4()),
    }

# ---------------- MongoDB collections ----------------
# The second tuple item is intentionally empty: production data is created and
# updated through the authenticated APIs, never through hardcoded seed records.
COLLECTIONS = {
    "employees": ("erp_employees", [], "EMP"),
    "customers": ("erp_customers", [], "CUS"),
    "bookings": ("erp_bookings", [], "BKG"),
    "projects": ("erp_projects", [], "PRJ"),
    "invoices": ("erp_invoices", [], "INV"),
    "documents": ("erp_documents", [], "DOC"),
    "tickets": ("erp_tickets", [], "TKT"),
    "services": ("erp_services", [], "SVC"),
    "gst": ("erp_gst", [], "GST"),
    "itr": ("erp_itr", [], "ITR"),
    "tds": ("erp_tds", [], "TDS"),
    "roc": ("erp_roc", [], "ROC"),
    "attendance": ("erp_attendance", [], "ATT"),
    "leaves": ("erp_leaves", [], "LV"),
    "tasks": ("erp_tasks", [], "TSK"),
    "payslips": ("erp_payslips", [], "PAY"),
    "leads": ("erp_leads", [], "LEAD"),
    "appointments": ("erp_appointments", [], "APT"),
    "commissions": ("erp_commissions", [], "COM"),
    "journal": ("erp_journal", [], "JE"),
    "payments": ("erp_payments", [], "PMT"),
    "agents": ("erp_agents", [], "AG"),
    "site-images": ("erp_site_images", [], "IMG"),
}

PERSONAL_EMPLOYEE_COLLECTIONS = {"attendance", "leaves", "tasks", "payslips"}
CUSTOMER_SCOPED_COLLECTIONS = {"customers", "bookings", "invoices", "documents", "payments", "tickets", "projects", "gst", "itr", "tds", "roc"}
AGENT_SCOPED_COLLECTIONS = {"leads", "customers", "bookings", "projects", "appointments", "commissions", "documents"}


def build_erp_router(db, get_current_user):
    router = APIRouter(prefix="/api")

    async def _notify(role, title, description, category, ntype="information", priority=None, user_id=None):
        prio = priority or {"urgent": "High", "warning": "Medium", "information": "Low"}.get(ntype, "Low")
        targets = [user_id] if user_id else [u.get("id") async for u in db.users.find({"role": role}, {"_id": 0, "id": 1})]
        if not targets and role in {"admin", "all"}:
            targets = [None]
        docs = [{
            "id": f"NTF-{uuid.uuid4().hex[:8].upper()}", "role": role, "user_id": uid, "title": title,
            "description": description, "category": category, "type": ntype,
            "priority": prio, "read": False, "ts": datetime.now(timezone.utc).isoformat(),
        } for uid in targets]
        if docs:
            await db["erp_notifications"].insert_many(docs)

    async def _customer_user_by_id(customer_id):
        if not customer_id:
            return None
        return await db.users.find_one({"role": "customer", "$or": [{"id": customer_id}, {"meta.customer_id": customer_id}]}, {"_id": 0})

    async def _agent_user_by_name(agent_name):
        return await db.users.find_one({"role": "agent", "name": agent_name}, {"_id": 0})

    async def _customer_record_for_user(user: dict):
        """Resolve the canonical ERP customer record for a logged-in customer."""
        if user.get("role") != "customer":
            return None
        meta_id = (user.get("meta") or {}).get("customer_id")
        if meta_id:
            record = await db["erp_customers"].find_one({"id": meta_id}, {"_id": 0})
            if record:
                return record
        clauses = []
        if user.get("email"):
            clauses.append({"email": user.get("email")})
        if user.get("mobile"):
            clauses.append({"mobile": user.get("mobile")})
        if clauses:
            record = await db["erp_customers"].find_one({"$or": clauses}, {"_id": 0})
            if record:
                await db.users.update_one({"id": user.get("id")}, {"$set": {"meta.customer_id": record["id"]}})
                return record
        return None

    async def _resolve_customer_id(value=None, *, name=None):
        """Return the ERP customer's stable `id`; never persist a display name as a FK."""
        if value:
            value = str(value).strip()
            record = await db["erp_customers"].find_one({"id": value}, {"_id": 0})
            if record:
                return record["id"]
            # Accept a customer portal user id only at the API boundary and
            # immediately translate it to the canonical ERP customer id.
            user = await db.users.find_one(
                {"role": "customer", "$or": [{"id": value}, {"meta.customer_id": value}]},
                {"_id": 0},
            )
            if user:
                record = await _customer_record_for_user(user)
                if record:
                    return record["id"]
        if name:
            record = await db["erp_customers"].find_one({"business_name": str(name).strip()}, {"_id": 0})
            if record:
                return record["id"]
        return None

    async def _resolve_service_id(value=None, *, name=None):
        """Resolve the canonical service record id from an API payload."""
        if value:
            value = str(value).strip()
            service = await db["erp_services"].find_one({"id": value}, {"_id": 0})
            if service:
                return service["id"]
        if name:
            wanted = str(name).strip()
            service = await db["erp_services"].find_one(
                {"$or": [{"name": wanted}, {"title": wanted}]}, {"_id": 0}
            )
            if service:
                return service["id"]
        return None

    async def _resolve_agent_id(value=None, *, name=None):
        """Resolve the canonical agent record id from an API payload."""
        if value:
            value = str(value).strip()
            agent = await db["erp_agents"].find_one({"id": value}, {"_id": 0})
            if agent:
                return agent["id"]
        if name:
            agent = await db["erp_agents"].find_one({"name": str(name).strip()}, {"_id": 0})
            if agent:
                return agent["id"]
        return None


    async def _sync_customer_dependents(customer_id: str, customer: dict = None):
        """Propagate customer display fields while keeping customer_id authoritative."""
        if not customer_id:
            return
        customer = customer or await db["erp_customers"].find_one({"id": customer_id}, {"_id": 0})
        if not customer:
            return
        display = customer.get("business_name") or customer.get("name") or customer_id
        # Foreign keys remain customer_id; these are denormalized display fields only.
        for collection in ("erp_bookings", "erp_invoices", "erp_payments", "erp_projects", "erp_documents", "erp_tickets"):
            await db[collection].update_many({"customer_id": customer_id}, {"$set": {"customer": display}})

    async def _sync_customer_gst_record(customer: dict):
        """Requirement: a customer marked as a GST customer must automatically
        appear in Admin -> GST Module, reusing the existing customer_id/GSTIN
        rather than creating a second, disconnected customer record.

        Keyed by customer_id (one GST record per customer): created the first
        time the customer's service is GST, and kept in sync (name/GSTIN) on
        every subsequent customer edit. Never deletes the GST record if the
        customer's service later changes, so filed-return history is preserved."""
        customer_id = customer.get("id")
        if not customer_id:
            return
        service_type = str(customer.get("service_type") or "").strip().lower()
        if service_type != "gst":
            return
        display = customer.get("business_name") or customer.get("owner") or customer_id
        update = {
            "client": display,
            "gstin": customer.get("gst_number") or "",
            "customer_id": customer_id,
        }
        existing_gst = await db["erp_gst"].find_one({"customer_id": customer_id}, {"_id": 0, "id": 1})
        if existing_gst:
            await db["erp_gst"].update_one({"id": existing_gst["id"]}, {"$set": update})
        else:
            gst_id = f"GST-{uuid.uuid4().hex[:6].upper()}"
            await db["erp_gst"].insert_one({
                "id": gst_id, **update,
                "return_type": "GSTR-1", "fy": "", "period": "", "due_date": "",
                "filed_date": "", "ack": "", "consultant": customer.get("assigned_employee") or "",
                "status": "Pending",
            })

    async def _sync_agent_dependents(agent_id: str, agent: dict = None):
        """Propagate agent display fields without touching attendance/account status."""
        if not agent_id:
            return
        agent = agent or await db["erp_agents"].find_one({"id": agent_id}, {"_id": 0})
        if not agent:
            return
        name = agent.get("name") or agent.get("agent_id") or agent_id
        await db["erp_bookings"].update_many({"agent_id": agent_id}, {"$set": {"assigned_agent": name, "agent": name}})
        await db["erp_commissions"].update_many({"agent_id": agent_id}, {"$set": {"agent": name, "agent_name": name}})
        await db["erp_payments"].update_many({"agent_id": agent_id}, {"$set": {"agent": name}})

    async def _sync_service_dependents(service_id: str, service: dict = None):
        """Propagate service display fields; service_id remains the canonical FK."""
        if not service_id:
            return
        service = service or await db["erp_services"].find_one({"id": service_id}, {"_id": 0})
        if not service:
            return
        name = service.get("name") or service.get("title") or service_id
        for collection in ("erp_bookings", "erp_invoices", "erp_payments"):
            await db[collection].update_many({"service_id": service_id}, {"$set": {"service": name}})

    async def _sync_invoice_dependents(invoice: dict):
        """Keep invoice-linked payments and booking relationships consistent."""
        if not invoice:
            return
        refs = [x for x in (invoice.get("invoice_no"), invoice.get("id")) if x]
        if not refs:
            return
        query = {"$or": [{"invoice_no": str(x)} for x in refs] + [{"invoice_id": str(x)} for x in refs]}
        update = {
            "invoice_id": invoice.get("id"),
            "invoice_no": invoice.get("invoice_no") or invoice.get("id"),
            "customer_id": invoice.get("customer_id"),
            "customer": invoice.get("customer"),
            "service_id": invoice.get("service_id"),
            "service": invoice.get("service"),
            "booking_id": invoice.get("booking_id"),
        }
        await db["erp_payments"].update_many(query, {"$set": update})

    async def _normalize_relationships(name, body: dict, existing: dict = None):
        """Attach/validate Phase-4 foreign keys while retaining display fields for UI."""
        current = existing or {}

        if name == "customers":
            if "payment_frequency" in body and body.get("payment_frequency") not in (None, "", "Monthly", "Quarterly", "Yearly"):
                raise HTTPException(status_code=422, detail="payment_frequency must be Monthly, Quarterly or Yearly")
            if body.get("service_id"):
                sid = await _resolve_service_id(str(body.get("service_id")))
                if not sid:
                    raise HTTPException(status_code=422, detail="Invalid service_id")
                body["service_id"] = sid
                service = await db["erp_services"].find_one({"id": sid}, {"_id": 0})
                if service:
                    body["service_type"] = service.get("category") or service.get("name") or body.get("service_type")
            elif body.get("service_type"):
                sid = await _resolve_service_id(None, name=body.get("service_type"))
                if sid:
                    body["service_id"] = sid
            if body.get("filing_status") and body.get("filing_status") not in {"Paid", "Pending", "Processing", "Completed"}:
                raise HTTPException(status_code=422, detail="Invalid customer payment status")
            agent_name = body.get("assigned_agent") or current.get("assigned_agent")
            if body.get("agent_id") or current.get("agent_id") or agent_name:
                aid = await _resolve_agent_id(
                    body.get("agent_id") if "agent_id" in body else current.get("agent_id"),
                    name=agent_name,
                )
                if aid:
                    body["agent_id"] = aid
                    agent = await db["erp_agents"].find_one({"id": aid}, {"_id": 0})
                    if agent:
                        body.setdefault("assigned_agent", agent.get("name"))
                        # Per-customer commission: default from the agent's own rate the
                        # first time this customer is assigned, but never overwrite a
                        # percentage this specific customer already has — the same agent
                        # can hold different percentages across different customers.
                        no_override = body.get("agent_commission_percentage") in (None, "")
                        no_existing = current.get("agent_commission_percentage") in (None, "", None)
                        if no_override and no_existing and agent.get("commission_percentage") not in (None, ""):
                            body["agent_commission_percentage"] = agent.get("commission_percentage")

        if name in {"bookings", "invoices"}:
            cid = await _resolve_customer_id(
                body.get("customer_id") if "customer_id" in body else current.get("customer_id"),
                name=body.get("customer") or current.get("customer"),
            )
            if cid:
                body["customer_id"] = cid
                customer_record = await db["erp_customers"].find_one({"id": cid}, {"_id": 0, "business_name": 1})
                if customer_record and not body.get("customer"):
                    body["customer"] = customer_record.get("business_name")
            elif name in {"bookings", "invoices"}:
                raise HTTPException(status_code=422, detail="A valid customer_id is required")

        if name in {"bookings", "invoices"}:
            service_name = body.get("service") or body.get("service_name") or current.get("service") or current.get("service_name")
            sid = await _resolve_service_id(
                body.get("service_id") if "service_id" in body else current.get("service_id"),
                name=service_name,
            )
            if sid:
                body["service_id"] = sid
                service = await db["erp_services"].find_one({"id": sid}, {"_id": 0})
                if service:
                    body.setdefault("service", service.get("name") or service.get("title"))
            else:
                raise HTTPException(status_code=422, detail="A valid service_id is required")

        if name == "bookings":
            agent_name = body.get("assigned_agent") or body.get("agent") or current.get("assigned_agent") or current.get("agent")
            if body.get("agent_id") or current.get("agent_id") or agent_name:
                aid = await _resolve_agent_id(
                    body.get("agent_id") if "agent_id" in body else current.get("agent_id"),
                    name=agent_name,
                )
                if aid:
                    body["agent_id"] = aid
                    agent = await db["erp_agents"].find_one({"id": aid}, {"_id": 0})
                    if agent and agent.get("name"):
                        body["assigned_agent"] = agent["name"]
                elif body.get("agent_id") or current.get("agent_id") or agent_name:
                    raise HTTPException(status_code=422, detail="A valid agent_id is required when a booking is assigned to an agent")

        if name == "invoices" and (body.get("booking_id") or current.get("booking_id")):
            booking_id = body.get("booking_id") or current.get("booking_id")
            booking = await db["erp_bookings"].find_one({"id": str(booking_id)}, {"_id": 0})
            if not booking:
                raise HTTPException(status_code=422, detail="Invalid booking_id")
            body["booking_id"] = booking["id"]
            # Keep invoice customer/service aligned with its booking.
            if booking.get("customer_id"):
                body["customer_id"] = booking["customer_id"]
            if booking.get("service_id"):
                body["service_id"] = booking["service_id"]
            if booking.get("service"):
                body["service"] = booking["service"]

        if name == "payments":
            invoice_ref = body.get("invoice_id") or current.get("invoice_id")
            invoice_no = body.get("invoice_no") or current.get("invoice_no")
            invoice = None
            if invoice_ref:
                invoice = await db["erp_invoices"].find_one({"id": str(invoice_ref)}, {"_id": 0})
            if not invoice and invoice_no:
                invoice = await db["erp_invoices"].find_one({"invoice_no": str(invoice_no)}, {"_id": 0})
            if invoice:
                body["invoice_id"] = invoice["id"]
                body["invoice_no"] = invoice.get("invoice_no") or invoice["id"]
                body["customer_id"] = invoice.get("customer_id")
                body["booking_id"] = invoice.get("booking_id")
                body["service_id"] = invoice.get("service_id")
                if invoice.get("service"):
                    body["service"] = invoice["service"]
            elif invoice_ref or invoice_no:
                raise HTTPException(status_code=404, detail="Invoice not found")
            agent_name = body.get("agent") or current.get("agent")
            if body.get("agent_id") or current.get("agent_id") or agent_name:
                aid = await _resolve_agent_id(
                    body.get("agent_id") if "agent_id" in body else current.get("agent_id"),
                    name=agent_name,
                )
                if aid:
                    body["agent_id"] = aid
                    agent = await db["erp_agents"].find_one({"id": aid}, {"_id": 0})
                    if agent and agent.get("name"):
                        body["agent"] = agent["name"]
                elif body.get("agent_id") or current.get("agent_id"):
                    raise HTTPException(status_code=422, detail="Invalid agent_id")

        if name == "commissions":
            agent_name = body.get("agent") or body.get("agent_name") or current.get("agent") or current.get("agent_name")
            if body.get("agent_id") or current.get("agent_id") or agent_name:
                aid = await _resolve_agent_id(
                    body.get("agent_id") if "agent_id" in body else current.get("agent_id"),
                    name=agent_name,
                )
                if aid:
                    body["agent_id"] = aid
                    agent = await db["erp_agents"].find_one({"id": aid}, {"_id": 0})
                    if agent and agent.get("name"):
                        body["agent"] = agent["name"]
                elif body.get("agent_id") or current.get("agent_id"):
                    raise HTTPException(status_code=422, detail="Invalid agent_id")

    async def _employee_record_for_user(user: dict):
        """Resolve the erp_employees document that belongs to a logged-in employee user."""
        if user.get("role") != "employee":
            return None
        emp_id = (user.get("meta") or {}).get("employee_id")
        if emp_id:
            emp = await db["erp_employees"].find_one({"id": emp_id}, {"_id": 0})
            if emp:
                return emp
        mobile = user.get("mobile")
        if mobile:
            emp = await db["erp_employees"].find_one({"mobile": mobile}, {"_id": 0})
            if emp:
                return emp
        return None

    async def _sync_employee_user(emp: dict, password_hash: str = None):
        """Keep the auth `users` record for an employee in sync with their erp_employees profile.

        password_hash is optional: when the admin supplies a password on Add/Edit
        Employee it is hashed by the caller and passed in here; when omitted, an
        existing account's password is left untouched (never cleared to None) and a
        new account is created exactly as before (password_hash: None, so the
        employee still logs in via mobile/OTP or a later self-registration/reset)."""
        mobile = emp.get("mobile")
        if not mobile:
            return
        update = {
            "name": emp.get("name") or "Employee",
            "meta.employee_id": emp["id"],
            "meta.department": emp.get("department"),
            "meta.designation": emp.get("designation"),
            "meta.manager": emp.get("manager"),
        }
        if password_hash:
            update["password_hash"] = password_hash
        existing = await db.users.find_one({"mobile": mobile, "role": "employee"})
        if existing:
            await db.users.update_one({"id": existing["id"]}, {"$set": update})
        else:
            await db.users.insert_one({
                "id": f"EMPLOYEE-{uuid.uuid4().hex[:8].upper()}",
                "name": emp.get("name") or "Employee",
                "email": emp.get("email"),
                "mobile": mobile,
                "role": "employee",
                "password_hash": password_hash,
                "avatar": None,
                "meta": {
                    "employee_id": emp["id"],
                    "department": emp.get("department"),
                    "designation": emp.get("designation"),
                    "manager": emp.get("manager"),
                    "portal_login": True,
                },
            })

    # ---------------- self-service employee profile ----------------
    @router.get("/employees/me")
    async def get_my_employee_profile(user: dict = Depends(get_current_user)):
        if user.get("role") != "employee":
            raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
        emp = await db["erp_employees"].find_one({"email": user.get("email")}, {"_id": 0})
        if not emp:
            raise HTTPException(status_code=404, detail="No employee profile found for this account yet")
        return _ok(emp)

    @router.put("/employees/me")
    async def update_my_employee_profile(body: dict = Body(...), user: dict = Depends(get_current_user)):
        if user.get("role") != "employee":
            raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
        emp = await _employee_record_for_user(user)
        if not emp:
            raise HTTPException(status_code=404, detail="No employee profile found for this account yet")
        # Employees can only edit their own contact/address details, not HR fields.
        allowed_fields = {"email", "address", "state", "avatar"}
        update = {k: v for k, v in body.items() if k in allowed_fields}
        if update:
            await db["erp_employees"].update_one({"id": emp["id"]}, {"$set": update})
            emp = await db["erp_employees"].find_one({"id": emp["id"]}, {"_id": 0})
        return _ok(emp, message="Profile updated")

    async def _sync_customer_service_records(customer: dict):
        """Keep service modules connected to the canonical customer record.

        Existing filing/return history is preserved. Missing service records are
        created only when the customer's primary service matches the module.
        customer_id is the authoritative foreign key.
        """
        customer_id = customer.get("id")
        if not customer_id:
            return
        service_type = str(customer.get("service_type") or "").strip().lower()
        display = customer.get("business_name") or customer.get("owner") or customer_id
        mappings = {
            "gst": ("erp_gst", "client", {
                "customer_id": customer_id, "client": display,
                "gstin": customer.get("gst_number") or "",
                "return_type": "GSTR-1", "fy": "", "period": "",
                "due_date": "", "filed_date": "", "ack": "",
                "consultant": customer.get("assigned_employee") or "", "status": "Pending",
            }),
            "income tax": ("erp_itr", "client", {
                "customer_id": customer_id, "client": display,
                "pan": customer.get("pan") or "", "ay": "2026-27",
                "return_no": "ITR-3", "due_date": "", "filed_date": "",
                "ack": "", "consultant": customer.get("assigned_employee") or "", "status": "Pending",
            }),
            "tds": ("erp_tds", "client", {
                "customer_id": customer_id, "client": display,
                "pan": customer.get("pan") or "", "form": "24Q", "quarter": "Q1",
                "fy": "2026-27", "due_date": "", "filed_date": "",
                "challan": "", "status": "Pending",
            }),
            "roc": ("erp_roc", "company", {
                "customer_id": customer_id, "company": display,
                "cin": customer.get("cin") or "", "form": "AOC-4",
                "fy": "2025-26", "due_date": "", "filed_date": "",
                "consultant": customer.get("assigned_employee") or "", "status": "Pending",
            }),
        }
        entry = mappings.get(service_type)
        if not entry:
            return
        collection, display_key, defaults = entry
        existing = await db[collection].find_one({"customer_id": customer_id}, {"_id": 0, "id": 1})
        if existing:
            update = {
                display_key: display,
                "customer_id": customer_id,
            }
            if collection == "erp_gst":
                update["gstin"] = customer.get("gst_number") or ""
            elif collection in {"erp_itr", "erp_tds"}:
                update["pan"] = customer.get("pan") or ""
            elif collection == "erp_roc":
                update["cin"] = customer.get("cin") or ""
            await db[collection].update_one({"id": existing["id"]}, {"$set": update})
            return
        defaults["id"] = f"{service_type[:3].upper()}-{uuid.uuid4().hex[:6].upper()}"
        await db[collection].insert_one(defaults)

    @router.post("/admin/workflows/drag-drop")
    async def drag_drop_workflow(body: dict = Body(...), user: dict = Depends(get_current_user)):
        """Validated Phase-5 drag/drop mutations.

        Drag/drop is only a UI affordance; this endpoint is the authoritative
        mutation boundary. It validates the source, target, role and existing
        relationships before changing MongoDB.
        """
        if user.get("role") not in {"admin", "super_admin", "superadmin"}:
            raise HTTPException(status_code=403, detail="Only administrators can perform workflow reassignment")

        source_type = str(body.get("source_type") or "").strip().lower()
        target_type = str(body.get("target_type") or "").strip().lower()
        source_id = str(body.get("source_id") or "").strip()
        target_value = body.get("target_value")
        if not source_type or not target_type or not source_id or target_value in (None, ""):
            raise HTTPException(status_code=400, detail="source_type, target_type, source_id and target_value are required")

        allowed = {
            ("customer", "service"),
            ("customer", "payment-status"),
            ("customer", "payment-frequency"),
            ("customer", "status"),
            ("customer", "agent"),
            ("booking", "status"),
            ("payment", "service"),
            ("payment", "payment-status"),
            ("booking", "service"),
        }
        if (source_type, target_type) not in allowed:
            raise HTTPException(status_code=400, detail="This workflow relationship is not supported")

        collection_by_source = {"customer": "erp_customers", "payment": "erp_payments", "booking": "erp_bookings"}
        collection = collection_by_source[source_type]
        source = await db[collection].find_one({"id": source_id}, {"_id": 0})
        if not source:
            raise HTTPException(status_code=404, detail=f"{source_type.title()} record not found")

        before = {}
        target_display = str(target_value)

        if target_type == "service":
            service_id = await _resolve_service_id(str(target_value))
            if not service_id:
                raise HTTPException(status_code=422, detail="A valid service_id is required")
            service = await db["erp_services"].find_one({"id": service_id}, {"_id": 0})
            if not service:
                raise HTTPException(status_code=404, detail="Service not found")
            service_name = service.get("name") or service.get("title") or service_id
            # The displayed service_type/category is whatever Admin set on the
            # service record — never forced back into a fixed enum. Section 2/6/12:
            # a brand-new service (e.g. "Trademark Registration") must be usable
            # here the moment it's created, with no code change and no loss of
            # its real category. service_id remains the authoritative FK either way.
            category = str(service.get("category") or service_name).strip() or "Other"
            if source_type == "customer":
                before = {"service_id": source.get("service_id"), "service_type": source.get("service_type")}
                if str(source.get("service_id") or "") == service_id and str(source.get("service_type") or "") == category:
                    return _ok(source, message="No change needed — customer is already assigned to this service")
                update = {"service_id": service_id, "service_type": category}
            elif source_type == "booking":
                before = {"service_id": source.get("service_id"), "service": source.get("service")}
                if str(source.get("service_id") or "") == service_id:
                    return _ok(source, message="No change needed — booking is already assigned to this service")
                update = {"service_id": service_id, "service": service_name}
            else:  # payment
                if source.get("invoice_id") or source.get("invoice_no"):
                    raise HTTPException(status_code=409, detail="Invoice-linked payments inherit their service from the invoice and cannot be reassigned directly")
                before = {"service_id": source.get("service_id"), "service": source.get("service")}
                if str(source.get("service_id") or "") == service_id:
                    return _ok(source, message="No change needed — payment is already assigned to this service")
                update = {"service_id": service_id, "service": service_name}
            await db[collection].update_one({"id": source_id}, {"$set": update})
            message = f"{source_type.title()} assigned to {service_name}"

        elif target_type == "payment-frequency":
            if source_type != "customer":
                raise HTTPException(status_code=400, detail="Payment frequency applies to customers only")
            valid = {"Monthly", "Quarterly", "Yearly"}
            if str(target_value) not in valid:
                raise HTTPException(status_code=400, detail="Invalid payment frequency")
            before = {"payment_frequency": source.get("payment_frequency")}
            update = {"payment_frequency": str(target_value)}
            if source.get("payment_frequency") == str(target_value):
                return _ok(source, message="No change needed — customer already has that payment frequency")
            await db[collection].update_one({"id": source_id}, {"$set": update})
            message = f"Customer payment frequency updated to {target_value}"
        elif target_type == "agent":
            if source_type != "customer":
                raise HTTPException(status_code=400, detail="Agent assignment applies to customers only")
            agent_id = await _resolve_agent_id(str(target_value))
            if not agent_id:
                raise HTTPException(status_code=422, detail="A valid agent_id is required")
            agent = await db["erp_agents"].find_one({"id": agent_id}, {"_id": 0})
            if not agent:
                raise HTTPException(status_code=404, detail="Agent not found")
            before = {"agent_id": source.get("agent_id"), "assigned_agent": source.get("assigned_agent"), "agent_commission_percentage": source.get("agent_commission_percentage")}
            if source.get("agent_id") == agent_id:
                return _ok(source, message="No change needed — customer is already assigned to this agent")
            update = {"agent_id": agent_id, "assigned_agent": agent.get("name")}
            # Same per-customer percentage rule as the Add/Edit Customer form: default
            # from the agent's own rate only if this customer doesn't already have one.
            if source.get("agent_commission_percentage") in (None, "") and agent.get("commission_percentage") not in (None, ""):
                update["agent_commission_percentage"] = agent.get("commission_percentage")
            await db[collection].update_one({"id": source_id}, {"$set": update})
            message = f"Customer assigned to agent {agent.get('name') or agent_id}"
        elif target_type == "status":
            if source_type == "customer":
                valid = {"Active", "Inactive"}
                if str(target_value) not in valid:
                    raise HTTPException(status_code=400, detail="Invalid customer status")
                before = {"status": source.get("status")}
                update = {"status": str(target_value)}
            elif source_type == "booking":
                valid = {"Pending", "Running", "Confirmed", "Completed"}
                if str(target_value) not in valid:
                    raise HTTPException(status_code=400, detail="Invalid booking status")
                before = {"status": source.get("status")}
                update = {"status": str(target_value)}
            else:
                raise HTTPException(status_code=400, detail="Status drag/drop is not supported for this record")
            if source.get("status") == str(target_value):
                return _ok(source, message="No change needed — record already has that status")
            await db[collection].update_one({"id": source_id}, {"$set": update})
            message = f"{source_type.title()} status updated to {target_value}"
        elif target_type == "payment-status":
            if source_type == "customer":
                valid_statuses = {"Paid", "Pending", "Processing", "Completed"}
                if str(target_value) not in valid_statuses:
                    raise HTTPException(status_code=400, detail="Invalid customer payment status")
                before = {"filing_status": source.get("filing_status")}
                if source.get("filing_status") == str(target_value):
                    return _ok(source, message="No change needed — customer already has that payment status")
                update = {"filing_status": str(target_value)}
            elif source_type == "payment":
                # These are the existing canonical backend payment statuses.
                valid_statuses = {"Completed", "Pending", "Failed"}
                if str(target_value) not in valid_statuses:
                    raise HTTPException(status_code=400, detail="Invalid payment status")
                before = {"status": source.get("status")}
                if source.get("status") == str(target_value):
                    return _ok(source, message="No change needed — payment already has that status")
                update = {"status": str(target_value)}
                amount = round(float(source.get("total") or source.get("amount") or 0), 2)
                if amount <= 0:
                    raise HTTPException(status_code=400, detail="Payment amount must be greater than zero")
                # Preserve existing invoice financial synchronization by using
                # the same rules as the normal payment update path.
            else:
                raise HTTPException(status_code=400, detail="Booking payment status drag/drop is not supported")

            await db[collection].update_one({"id": source_id}, {"$set": update})
            if source_type == "payment":
                invoice_refs = {str(x) for x in (source.get("invoice_no"), source.get("invoice_id")) if x}
                for inv_ref in invoice_refs:
                    invoice = await db["erp_invoices"].find_one({"$or": [{"invoice_no": inv_ref}, {"id": inv_ref}]}, {"_id": 0})
                    if invoice:
                        synced = await _sync_invoice_financials(invoice["id"])
                        await _sync_booking_payment_status(synced or invoice)
            message = f"{source_type.title()} payment status updated to {target_value}"

        updated = await db[collection].find_one({"id": source_id}, {"_id": 0})
        await _audit(user, "workflow_drag_drop", source_type, source_id, {
            "target_type": target_type, "target_value": target_value, "before": before,
            "after": {k: updated.get(k) for k in before.keys()} if updated else {},
        })
        return _ok(updated, message=message)

    def make_crud(name, coll, prefix):
        def guard_write(user):
            allowed = WRITE_ROLES.get(name, {"admin"})
            if user.get("role") not in allowed:
                raise HTTPException(status_code=403, detail=f"Your role is not permitted to modify {name}")

        is_personal = name in PERSONAL_EMPLOYEE_COLLECTIONS
        is_customer_scoped = name in CUSTOMER_SCOPED_COLLECTIONS

        def _owns_booking(item, user):
            # Canonical ownership is the ERP customer record id. The account id
            # is accepted only for legacy rows that predate Phase 4 migration.
            canonical = (user.get("meta") or {}).get("customer_id")
            return item.get("customer_id") in {canonical, user.get("id")}

        async def _employee_name(user):
            emp = await _employee_record_for_user(user)
            return emp.get("name") if emp else None

        async def _agent_name(user):
            if user.get("role") != "agent":
                return None
            agent_id = (user.get("meta") or {}).get("agent_id")
            if agent_id:
                agent = await db["erp_agents"].find_one({"id": agent_id}, {"_id": 0})
                if agent:
                    return agent.get("name")
            return user.get("name")

        async def _customer_profile(user):
            if user.get("role") != "customer":
                return None
            cid = (user.get("meta") or {}).get("customer_id")
            if cid:
                return await db["erp_customers"].find_one({"id": cid}, {"_id": 0})
            return await db["erp_customers"].find_one({"email": user.get("email")}, {"_id": 0})

        async def _audit(user, action, collection, item_id=None, changes=None):
            try:
                await db["erp_audit_logs"].insert_one({
                    "id": f"AUD-{uuid.uuid4().hex[:10].upper()}",
                    "action": action, "collection": collection, "item_id": item_id,
                    "user_id": user.get("id"), "role": user.get("role"),
                    "changes": changes or {}, "ts": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

        async def _enrich_customer_financials(items):
            if name != "customers" or not items:
                return items
            customer_ids = [str(i.get("id")) for i in items if i.get("id")]
            invoices = await db["erp_invoices"].find({"customer_id": {"$in": customer_ids}}, {"_id": 0}).to_list(10000)
            grouped = {}
            for inv in invoices:
                cid = str(inv.get("customer_id") or "")
                if not cid:
                    continue
                state = await _invoice_financial_state(inv)
                bucket = grouped.setdefault(cid, {"invoiced": 0.0, "paid": 0.0, "balance": 0.0, "invoice_count": 0})
                bucket["invoiced"] += float(inv.get("total") or 0)
                bucket["paid"] += float(state["paid_amount"])
                bucket["balance"] += float(state["balance"])
                bucket["invoice_count"] += 1
            for item in items:
                state = grouped.get(str(item.get("id")), {"invoiced": 0.0, "paid": 0.0, "balance": 0.0, "invoice_count": 0})
                item["invoiced_amount"] = round(state["invoiced"], 2)
                item["paid_amount"] = round(state["paid"], 2)
                item["outstanding"] = round(state["balance"], 2)
                item["invoice_count"] = state["invoice_count"]
            # Reuse the existing Security login-audit/session system. No second
            # customer activity tracker is created.
            ids = [str(i.get("id")) for i in items if i.get("id")]
            users = await db.users.find({"meta.customer_id": {"$in": ids}}, {"_id": 0, "id": 1, "meta": 1}).to_list(2000)
            user_by_customer = {str((u.get("meta") or {}).get("customer_id")): u for u in users}
            user_ids = [u.get("id") for u in users if u.get("id")]
            audits = await db.login_audits.find({"user_id": {"$in": user_ids}, "event_type": "LOGIN_SUCCESS"}, {"_id": 0}).sort("created_at", -1).to_list(5000) if user_ids else []
            latest = {}
            for a in audits:
                uid = a.get("user_id")
                if uid not in latest:
                    latest[uid] = a
            bookings = await db["erp_bookings"].find({"customer_id": {"$in": ids}}, {"_id": 0, "customer_id": 1, "status": 1, "booking_date": 1}).sort("booking_date", -1).to_list(5000)
            latest_booking = {}
            for b in bookings:
                cid = str(b.get("customer_id") or "")
                if cid and cid not in latest_booking:
                    latest_booking[cid] = b
            for item in items:
                cid = str(item.get("id") or "")
                u = user_by_customer.get(cid)
                a = latest.get(u.get("id")) if u else None
                if a:
                    item["last_login"] = (a.get("login_time") or a.get("created_at")).isoformat() if hasattr((a.get("login_time") or a.get("created_at")), "isoformat") else str(a.get("login_time") or a.get("created_at"))
                    item["login_status"] = "Successful"
                    item["recent_login_activity"] = {"ip": a.get("ip_address"), "device": a.get("device"), "browser": a.get("browser")}
                else:
                    item["last_login"] = None
                    item["login_status"] = "No successful login recorded"
                    item["recent_login_activity"] = None
                b = latest_booking.get(cid)
                item["booking_status"] = b.get("status") if b else None
            return items

        @router.get(f"/{name}", name=f"list_{name}")
        async def list_items(search: str = Query(None), status: str = Query(None), page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500), user: dict = Depends(get_current_user)):
            if user.get("role") not in READ_ROLES.get(name, {"admin"}):
                raise HTTPException(status_code=403, detail=f"Your role is not permitted to view {name}")
            if name == "employees" and user.get("role") not in ("admin", "employee"):
                raise HTTPException(status_code=403, detail="You are not permitted to view employees")
            items = await db[coll].find({}, {"_id": 0}).to_list(2000)
            if is_personal and user.get("role") == "employee":
                emp = await _employee_record_for_user(user)
                emp_id = emp["id"] if emp else "__none__"
                items = [i for i in items if i.get("employee_id") == emp_id]
            if user.get("role") == "employee":
                emp = await _employee_record_for_user(user)
                emp_name = emp.get("name") if emp else None
                if name == "employees":
                    items = [i for i in items if emp and i.get("id") == emp["id"]]
                elif name == "customers":
                    items = [i for i in items if emp_name and i.get("assigned_employee") == emp_name]
                elif name in {"projects", "appointments"}:
                    items = [i for i in items if emp_name and (i.get("assigned_employee") == emp_name or i.get("employee") == emp_name)]
                elif name == "documents":
                    items = [i for i in items if emp_name and (i.get("uploaded_by") == emp_name or i.get("assigned_employee") == emp_name)]
            if user.get("role") == "agent" and name in AGENT_SCOPED_COLLECTIONS:
                agent_name = await _agent_name(user)
                agent_id = (user.get("meta") or {}).get("agent_id")
                items = [i for i in items if
                         (agent_id and i.get("agent_id") == agent_id) or
                         (agent_name and (i.get("assigned_agent") == agent_name or i.get("agent") == agent_name or i.get("agent_name") == agent_name))]
                if name == "commissions" and agent_name:
                    items = [i for i in items if not i.get("agent_id") or i.get("agent_id") == agent_id]
            if is_customer_scoped and user.get("role") == "customer":
                if name == "customers":
                    profile = await _customer_profile(user)
                    items = [profile] if profile else []
                else:
                    items = [i for i in items if _owns_booking(i, user)]
            if search:
                s = search.lower()
                items = [i for i in items if any(s in str(v).lower() for v in i.values())]
            if status and status != "all":
                items = [i for i in items if str(i.get("status", "")).lower() == status.lower()]
            if name == "invoices":
                enriched = []
                for inv in items:
                    state = await _invoice_financial_state(inv)
                    inv.update({"paid_amount": state["paid_amount"], "balance": state["balance"], "payment_status": state["payment_status"]})
                    enriched.append(inv)
                items = enriched
            if name == "customers":
                items = await _enrich_customer_financials(items)
            total = len(items)
            start = (page - 1) * page_size
            items = items[start:start + page_size]
            return _ok(items, pagination={"page": page, "page_size": page_size, "total": total, "pages": (total + page_size - 1) // page_size})

        @router.get(f"/{name}/{{item_id}}", name=f"get_{name}")
        async def get_item(item_id: str, user: dict = Depends(get_current_user)):
            item = await db[coll].find_one({"id": item_id}, {"_id": 0})
            if not item:
                raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
            if user.get("role") not in READ_ROLES.get(name, {"admin"}):
                raise HTTPException(status_code=403, detail=f"Your role is not permitted to view {name}")
            if user.get("role") == "employee" and (is_personal or name == "employees"):
                emp = await _employee_record_for_user(user)
                own_id = emp["id"] if emp else None
                owner_id = item.get("employee_id") if is_personal else item.get("id")
                if not own_id or owner_id != own_id:
                    raise HTTPException(status_code=403, detail="You can only view your own records")
            elif name == "employees" and user.get("role") not in ("admin",):
                raise HTTPException(status_code=403, detail="You are not permitted to view this employee")
            elif user.get("role") == "employee" and name in {"customers", "projects", "appointments", "documents"}:
                emp = await _employee_record_for_user(user); emp_name = emp.get("name") if emp else None
                allowed = (name == "customers" and item.get("assigned_employee") == emp_name) or (name in {"projects", "appointments"} and (item.get("assigned_employee") == emp_name or item.get("employee") == emp_name)) or (name == "documents" and (item.get("uploaded_by") == emp_name or item.get("assigned_employee") == emp_name))
                if not allowed:
                    raise HTTPException(status_code=404, detail=f"{name[:-1].title()} not found")
            elif user.get("role") == "agent" and name in AGENT_SCOPED_COLLECTIONS:
                agent_name = await _agent_name(user)
                agent_id = (user.get("meta") or {}).get("agent_id")
                if not ((agent_id and item.get("agent_id") == agent_id) or (agent_name and (item.get("assigned_agent") == agent_name or item.get("agent") == agent_name or item.get("agent_name") == agent_name))):
                    raise HTTPException(status_code=404, detail=f"{name[:-1].title()} not found")
            elif name == "customers" and user.get("role") == "customer":
                profile = await _customer_profile(user)
                if not profile or profile.get("id") != item.get("id"):
                    raise HTTPException(status_code=404, detail="Customer not found")
            elif is_customer_scoped and user.get("role") == "customer" and not _owns_booking(item, user):
                raise HTTPException(status_code=404, detail=f"{name[:-1].title()} not found")
            if name == "customers":
                enriched = await _enrich_customer_financials([item])
                item = enriched[0]
            elif name == "invoices":
                state = await _invoice_financial_state(item)
                item.update({"paid_amount": state["paid_amount"], "balance": state["balance"], "payment_status": state["payment_status"]})
            return _ok(item)

        async def _attach_customer_id(body: dict, user: dict):
            if name not in {"customers", "bookings", "projects", "invoices", "documents", "tickets", "payments", "gst", "itr", "tds", "roc"}:
                return
            if user.get("role") == "customer":
                customer = await _customer_record_for_user(user)
                if not customer:
                    raise HTTPException(status_code=404, detail="No customer profile is linked to this account")
                body["customer_id"] = customer["id"]
                return
            if body.get("customer_id"):
                resolved = await _resolve_customer_id(body.get("customer_id"))
                if not resolved:
                    raise HTTPException(status_code=422, detail="Invalid customer_id")
                body["customer_id"] = resolved
                return
            customer_name = body.get("customer") or body.get("client") or body.get("client_name") or body.get("company") or body.get("business_name")
            if customer_name:
                resolved = await _resolve_customer_id(name=customer_name)
                if resolved:
                    body["customer_id"] = resolved

        def _normalize_invoice(body: dict, existing: dict = None):
            if name != "invoices":
                return
            taxable = float(body.get("taxable", (existing or {}).get("taxable", 0)) or 0)
            discount = float(body.get("discount", (existing or {}).get("discount", 0)) or 0)
            rate = float(body.get("rate", (existing or {}).get("rate", 18)) or 18)
            if taxable < 0 or discount < 0 or discount > taxable or rate < 0 or rate > 100:
                raise HTTPException(status_code=400, detail="Invalid taxable amount, discount, or GST rate")
            net = round(taxable - discount, 2)
            gstin = body.get("gst_number", (existing or {}).get("gst_number", ""))
            # Same-state vs interstate is represented by the existing `istate` flag;
            # default to CGST/SGST for local invoices.
            if body.get("istate", (existing or {}).get("istate", False)):
                cgst = sgst = 0; igst = round(net * rate / 100, 2)
            else:
                cgst = round(net * (rate / 2) / 100, 2); sgst = cgst; igst = 0
            total = round(net + cgst + sgst + igst, 2)
            body.update({"taxable": taxable, "discount": discount, "rate": rate, "cgst": cgst, "sgst": sgst, "igst": igst, "total": total})
            if not existing:
                body.setdefault("paid_amount", 0)
                body.setdefault("balance", total)
                body.setdefault("payment_status", "Pending")

        @router.post(f"/{name}", name=f"create_{name}")
        async def create_item(body: dict = Body(...), user: dict = Depends(get_current_user)):
            guard_write(user)
            await _attach_customer_id(body, user)
            await _normalize_relationships(name, body)
            if name == "documents":
                _validate_document_upload(body, user)
            _normalize_invoice(body)
            if name == "site-images":
                _validate_site_image(body)
                body.setdefault("status", "Active")
                body["created_at"] = datetime.now(timezone.utc).isoformat()
            emp = None
            if is_personal and user.get("role") == "employee":
                emp = await _employee_record_for_user(user)
                if not emp:
                    raise HTTPException(status_code=404, detail="No employee profile is linked to your account yet. Contact your admin.")
                # Employees may only ever create records under their own identity.
                body["employee_id"] = emp["id"]
                body["employee_name"] = emp.get("name")
                if name == "leaves":
                    body["status"] = "Pending"
                if name == "attendance":
                    # This is a Check In. Never trust a client-supplied date/time —
                    # always stamp with server-side IST time so it can't be spoofed
                    # and can't drift from the server's own duplicate-check below.
                    today = _ist_today_str()
                    existing_today = await db[coll].find_one(
                        {"employee_id": emp["id"], "date": today}, {"_id": 0}
                    )
                    if existing_today:
                        # Already checked in today — this is not an error condition,
                        # just return the existing record so the frontend can show
                        # the correct state instead of surfacing a failure.
                        return _ok(existing_today, message="You have already checked in today")
                    body["date"] = today
                    body["check_in"] = _ist_time_str()
                    body["check_out"] = "-"
                    body["hours"] = "-"
                    body.setdefault("status", "Present")

            if name == "payments":
                # Payments are authoritative financial events. When linked to an
                # invoice, validate the amount and synchronize the invoice in the
                # same request; duplicate IDs/references are rejected.
                amount = round(float(body.get("total") or body.get("amount") or 0), 2)
                if amount <= 0:
                    raise HTTPException(status_code=400, detail="Payment amount must be greater than zero")
                payment_id = str(body.get("payment_id") or body.get("id") or "").strip()
                if payment_id and await db[coll].find_one({"$or": [{"id": payment_id}, {"payment_id": payment_id}]}):
                    raise HTTPException(status_code=409, detail="A payment with this Payment ID already exists")
                invoice_no = str(body.get("invoice_no") or "").strip()
                if invoice_no:
                    invoice = await db["erp_invoices"].find_one({"$or": [{"invoice_no": invoice_no}, {"id": invoice_no}]}, {"_id": 0})
                    if not invoice:
                        raise HTTPException(status_code=404, detail="Invoice not found")
                    state = await _invoice_financial_state(invoice)
                    if state["payment_status"] == "Cancelled":
                        raise HTTPException(status_code=400, detail="Cancelled invoices cannot receive payments")
                    if amount > state["balance"] + 0.009:
                        raise HTTPException(status_code=400, detail=f"Payment exceeds invoice balance of ₹{state['balance']:.2f}")
                    ref = str(body.get("reference_no") or "").strip()
                    if ref and await db[coll].find_one({"invoice_no": invoice.get("invoice_no"), "reference_no": ref}):
                        raise HTTPException(status_code=409, detail="This payment reference is already recorded for the invoice")
                    body["invoice_no"] = invoice.get("invoice_no") or invoice.get("id")
                    body["amount"] = amount
                    body["total"] = amount
                    body.setdefault("customer_id", invoice.get("customer_id"))
                    body.setdefault("customer", invoice.get("customer"))
                    body.setdefault("booking_id", invoice.get("booking_id"))
                body["amount"] = amount
                body["total"] = amount
                body.setdefault("status", "Completed")
                if body["status"] not in {"Completed", "Pending", "Failed"}:
                    raise HTTPException(status_code=400, detail="Invalid payment status")
            new_password_hash = None
            if name == "employees":
                # Optional password: never persisted on the erp_employees profile
                # itself — only used to provision/update the real login account below.
                raw_password = body.pop("password", None)
                if raw_password:
                    if len(str(raw_password)) < 8:
                        raise HTTPException(status_code=422, detail="Password must be at least 8 characters long.")
                    new_password_hash = _hash_password(str(raw_password))
            if name in {"agents", "customers"}:
                _validate_percentage(body, "commission_percentage" if name == "agents" else "agent_commission_percentage")
            new_id = body.get("id")
            if name == "employees":
                # Keep the human-entered Employee ID (emp_id) as the canonical id
                # so it lines up with the ID shown in the UI and used for auth linking.
                candidate = body.get("emp_id") or body.get("id")
                if candidate and not await db[coll].find_one({"id": candidate}):
                    new_id = candidate
                else:
                    new_id = f"{prefix}-{uuid.uuid4().hex[:6].upper()}"
                body["emp_id"] = new_id
            elif not new_id or await db[coll].find_one({"id": new_id}):
                new_id = f"{prefix}-{uuid.uuid4().hex[:6].upper()}"
            body["id"] = new_id
            if name == "payments":
                body.setdefault("payment_id", new_id)

            if name == "bookings":
                body.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                if user.get("role") == "customer":
                    body["status"] = "Pending"
                    body["payment_status"] = "Pending"
                else:
                    body.setdefault("status", "Pending")
                    if body.get("status") not in {"Pending", "Running", "Confirmed", "Completed"}:
                        raise HTTPException(status_code=400, detail="Invalid booking status")
                    body.setdefault("payment_status", "Pending")
            await db[coll].insert_one({**body})
            body.pop("_id", None)
            if name == "bookings" and body.get("customer_id") and body.get("service_id"):
                # A booking is the source event for customer/service
                # synchronization. Keep the canonical customer account and
                # attach the service without creating another customer.
                service_doc = await db["erp_services"].find_one({"id": body["service_id"]}, {"_id": 0, "category": 1, "name": 1})
                await db["erp_customers"].update_one(
                    {"id": body["customer_id"]},
                    {"$set": {"service_id": body["service_id"], "service_type": (service_doc or {}).get("category") or (service_doc or {}).get("name") or body.get("service")}}
                )
                synced_customer = await db["erp_customers"].find_one({"id": body["customer_id"]}, {"_id": 0})
                await _sync_customer_service_records(synced_customer or {})
            if name == "invoices":
                synced_invoice = await _sync_invoice_financials(new_id)
                if synced_invoice:
                    body.update({k: synced_invoice.get(k) for k in ("paid_amount", "balance", "payment_status")})
            if name == "customers":
                await _sync_customer_gst_record(body)
                await _sync_customer_service_records(body)
            if name == "payments" and body.get("invoice_no") and body.get("status") in {"Completed", "Paid"}:
                invoice = await db["erp_invoices"].find_one({"$or": [{"invoice_no": body["invoice_no"]}, {"id": body["invoice_no"]}]}, {"_id": 0})
                if invoice:
                    try:
                        synced = await _sync_invoice_financials(invoice["id"])
                        await _sync_booking_payment_status(synced or invoice)
                    except Exception:
                        await db[coll].delete_one({"id": new_id})
                        raise HTTPException(status_code=500, detail="Payment could not be synchronized with the invoice")
            await _audit(user, "create", name, new_id, body)

            if name == "employees":
                # Provision / update the real auth account so this employee can log in.
                await _sync_employee_user(body, password_hash=new_password_hash)
            if name == "leaves" and user.get("role") == "employee":
                await _notify("admin", "New leave request", f"{body.get('employee_name', 'An employee')} requested {body.get('leave_type', 'leave')} ({body.get('from_date')} to {body.get('to_date')}).", "Attendance", "warning")
            if name == "bookings":
                cust = body.get("customer", "A customer"); svc = body.get("service", "a service")
                agent = body.get("assigned_agent", "your consultant"); prio = body.get("priority", "Low")
                await _notify("admin", f"New booking {new_id} received", f"{cust} requested {svc}. Priority: {prio}.", "Bookings", "warning")
                agent_user = await _agent_user_by_name(agent)
                cust_user = await _customer_user_by_id(body.get("customer_id"))
                await _notify("agent", f"New booking assigned: {new_id}", f"{cust} — {svc}. Due {body.get('due_date') or 'TBD'}.", "Bookings", "warning", user_id=(agent_user or {}).get("id"))
                await _notify("customer", f"Booking {new_id} submitted", f"{svc} assigned to {agent}. Status: {body.get('status', 'Pending')}.", "Bookings", "information", user_id=(cust_user or {}).get("id"))
            return _ok(body, message=f"{name[:-1].title()} created")

        @router.put(f"/{name}/{{item_id}}", name=f"update_{name}")
        async def update_item(item_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
            guard_write(user)
            existing = await db[coll].find_one({"id": item_id}, {"_id": 0})
            if not existing:
                raise HTTPException(status_code=404, detail="Not found")
            if name in {"bookings", "invoices", "payments", "commissions"}:
                await _normalize_relationships(name, body, existing)
            _normalize_invoice(body, existing)
            if name == "site-images":
                _validate_site_image(body, partial=True)

            emp = None
            if is_personal and user.get("role") == "employee":
                emp = await _employee_record_for_user(user)
                own_id = emp["id"] if emp else None
                if not own_id or existing.get("employee_id") != own_id:
                    raise HTTPException(status_code=403, detail="You can only update your own records")
                # Employees cannot approve/reject their own leave requests.
                if name == "leaves":
                    body.pop("status", None)
                body["employee_id"] = own_id
                if name == "attendance":
                    # This is a Check Out. Verify the employee actually checked in
                    # first, and always stamp the check-out time server-side rather
                    # than trusting whatever the browser sends.
                    check_in = existing.get("check_in")
                    if not check_in or check_in == "-":
                        raise HTTPException(status_code=400, detail="Please check in first.")
                    if existing.get("check_out") and existing.get("check_out") != "-":
                        # Already checked out today — return the existing record
                        # instead of failing or silently overwriting it.
                        return _ok(existing, message="You have already checked out today")
                    check_out = _ist_time_str()
                    body["check_out"] = check_out
                    body["hours"] = _hours_between(check_in, check_out)
                    body.setdefault("status", existing.get("status") or "Present")

            if name == "bookings" and "status" in body:
                if user.get("role") not in {"admin", "agent"}:
                    body.pop("status", None)
                elif str(body.get("status")) not in {"Pending", "Running", "Confirmed", "Completed"}:
                    raise HTTPException(status_code=400, detail="Invalid booking status")
            if is_customer_scoped and user.get("role") == "customer":
                if not _owns_booking(existing, user):
                    raise HTTPException(status_code=404, detail=f"{name[:-1].title()} not found")
                # Customers cannot re-assign ownership or fake payment/status state via edits.
                body.pop("customer_id", None)
                if name == "bookings":
                    body.pop("payment_status", None)

            if name == "payments":
                amount = round(float(body.get("total") or body.get("amount") or existing.get("total") or 0), 2)
                if amount <= 0:
                    raise HTTPException(status_code=400, detail="Payment amount must be greater than zero")
                body["amount"] = amount
                body["total"] = amount
                if "status" in body and body["status"] not in {"Completed", "Pending", "Failed"}:
                    raise HTTPException(status_code=400, detail="Invalid payment status")
                if body.get("invoice_no") or existing.get("invoice_no"):
                    inv_no = body.get("invoice_no") or existing.get("invoice_no")
                    invoice = await db["erp_invoices"].find_one({"$or": [{"invoice_no": inv_no}, {"id": inv_no}]}, {"_id": 0})
                    if invoice and body.get("status", existing.get("status")) in {"Completed", "Paid"}:
                        other = await _invoice_financial_state(invoice)
                        old_amount = float(existing.get("total") or existing.get("amount") or 0) if existing.get("status") in {"Completed", "Paid"} else 0
                        allowed_balance = other["balance"] + old_amount
                        if amount > allowed_balance + 0.009:
                            raise HTTPException(status_code=400, detail=f"Payment exceeds invoice balance of ₹{allowed_balance:.2f}")

            body.pop("_id", None); body.pop("id", None)
            update_password_hash = None
            if name == "employees":
                raw_password = body.pop("password", None)
                if raw_password:
                    if len(str(raw_password)) < 8:
                        raise HTTPException(status_code=422, detail="Password must be at least 8 characters long.")
                    update_password_hash = _hash_password(str(raw_password))
            if name in {"agents", "customers"}:
                _validate_percentage(body, "commission_percentage" if name == "agents" else "agent_commission_percentage")
            res = await db[coll].update_one({"id": item_id}, {"$set": body})
            if res.matched_count == 0:
                raise HTTPException(status_code=404, detail="Not found")
            item = await db[coll].find_one({"id": item_id}, {"_id": 0})
            if name == "bookings" and item.get("customer_id") and item.get("service_id"):
                service_doc = await db["erp_services"].find_one({"id": item["service_id"]}, {"_id": 0, "category": 1, "name": 1})
                await db["erp_customers"].update_one(
                    {"id": item["customer_id"]},
                    {"$set": {"service_id": item["service_id"], "service_type": (service_doc or {}).get("category") or (service_doc or {}).get("name") or item.get("service")}}
                )
                synced_customer = await db["erp_customers"].find_one({"id": item["customer_id"]}, {"_id": 0})
                await _sync_customer_service_records(synced_customer or {})
            if name == "customers":
                await _sync_customer_dependents(item_id, item)
                await _sync_customer_gst_record(item)
                await _sync_customer_service_records(item)
            if name == "agents":
                await _sync_agent_dependents(item_id, item)
            if name == "services":
                await _sync_service_dependents(item_id, item)
            if name == "invoices":
                item = await _sync_invoice_financials(item_id) or item
                await _sync_invoice_dependents(item)
                await _sync_booking_payment_status(item)
            if name == "payments":
                invoice_refs = {str(x) for x in (existing.get("invoice_no"), item.get("invoice_no")) if x}
                for inv_ref in invoice_refs:
                    invoice = await db["erp_invoices"].find_one({"$or": [{"invoice_no": inv_ref}, {"id": inv_ref}]}, {"_id": 0})
                    if invoice:
                        synced = await _sync_invoice_financials(invoice["id"])
                        await _sync_booking_payment_status(synced or invoice)
            await _audit(user, "update", name, item_id, body)

            if name == "employees":
                await _sync_employee_user(item, password_hash=update_password_hash)
            if name == "leaves" and body.get("status") and user.get("role") == "admin":
                st = body.get("status")
                await _notify("employee", f"Leave request {st.lower()}", f"Your {item.get('leave_type', 'leave')} request ({item.get('from_date')} to {item.get('to_date')}) was {st.lower()}.", "Attendance", "information" if st == "Approved" else "warning")
            if name == "bookings" and body.get("status"):
                st = body.get("status")
                await _notify("customer", f"Booking {item_id} update", f"Your booking status is now '{st}'.", "Bookings", "information")
                await _notify("admin", f"Booking {item_id} → {st}", f"Consultant updated booking {item_id} to '{st}'.", "Bookings", "information")
            return _ok(item, message=f"{name[:-1].title()} updated")

        @router.delete(f"/{name}/{{item_id}}", name=f"delete_{name}")
        async def delete_item(item_id: str, user: dict = Depends(get_current_user)):
            guard_write(user)
            if is_personal and user.get("role") == "employee":
                existing = await db[coll].find_one({"id": item_id}, {"_id": 0})
                emp = await _employee_record_for_user(user)
                own_id = emp["id"] if emp else None
                if not existing or not own_id or existing.get("employee_id") != own_id:
                    raise HTTPException(status_code=403, detail="You can only delete your own records")
            if is_customer_scoped and user.get("role") == "customer":
                existing = await db[coll].find_one({"id": item_id}, {"_id": 0})
                if not existing or not _owns_booking(existing, user):
                    raise HTTPException(status_code=404, detail=f"{name[:-1].title()} not found")
            if name == "customers":
                related = {}
                for label, collection in (("bookings", "erp_bookings"), ("invoices", "erp_invoices"), ("payments", "erp_payments")):
                    related[label] = await db[collection].count_documents({"customer_id": item_id})
                if any(related.values()):
                    raise HTTPException(status_code=409, detail="Customer cannot be deleted while bookings, invoices, or payments are linked to it")
            if name == "services":
                # Section 3: a service already connected to customers, invoices,
                # payments or bookings must never be hard-deleted — the historical
                # record (billing, filings, assignments) has to survive. Admins
                # should Activate/Deactivate/Archive it instead (status update),
                # which every consuming module (Customer/Employee/Agent) already
                # reads live from this same service record.
                related = {}
                for label, collection in (
                    ("customers", "erp_customers"), ("bookings", "erp_bookings"),
                    ("invoices", "erp_invoices"), ("payments", "erp_payments"),
                ):
                    related[label] = await db[collection].count_documents({"service_id": item_id})
                if any(related.values()):
                    parts = ", ".join(f"{v} {k}" for k, v in related.items() if v)
                    raise HTTPException(
                        status_code=409,
                        detail=f"This service is linked to {parts}. Deactivate or archive it instead of deleting to preserve historical records.",
                    )
            existing_payment = await db[coll].find_one({"id": item_id}, {"_id": 0}) if name == "payments" else None
            res = await db[coll].delete_one({"id": item_id})
            if res.deleted_count == 0:
                raise HTTPException(status_code=404, detail="Not found")
            if name == "payments" and existing_payment and existing_payment.get("invoice_no"):
                invoice = await db["erp_invoices"].find_one({"$or": [{"invoice_no": existing_payment["invoice_no"]}, {"id": existing_payment["invoice_no"]}]}, {"_id": 0})
                if invoice:
                    synced = await _sync_invoice_financials(invoice["id"])
                    await _sync_booking_payment_status(synced or invoice)
            await _audit(user, "delete", name, item_id)
            if name == "employees":
                await db.users.delete_one({"meta.employee_id": item_id, "role": "employee"})
            return _ok({"id": item_id}, message=f"{name[:-1].title()} deleted")

    for name, (coll, seed, prefix) in COLLECTIONS.items():
        make_crud(name, coll, prefix)

    # ---------------- Public (no-auth) service catalog ----------------
    # The generic `/services` route above requires a logged-in user, which
    # is correct for the portals — but the marketing Customer Site also
    # needs to list live, admin-created services for visitors who have not
    # signed in yet (section 5 "Customer Site: Service list" must reflect
    # whatever Admin just created/edited/deactivated, with no code change
    # and no login required). This mirrors list_items's read-only shape but
    # only exposes active/available services and public-safe fields.
    @router.get("/public/services")
    async def list_public_services():
        items = await db["erp_services"].find({}, {"_id": 0}).to_list(2000)
        visible = [s for s in items if str(s.get("status") or "Active").strip().lower() not in ("inactive", "archived", "hidden", "disabled")]
        fields = ("id", "name", "title", "category", "description", "short_description", "price", "discount", "final_price", "image", "banner_image", "icon", "status", "frequency", "pricing_type")
        return _ok([{k: s.get(k) for k in fields if k in s} for s in visible])

    @router.get("/services/{service_id}/stats")
    async def service_stats(service_id: str, user: dict = Depends(get_current_user)):
        """Section 11 'Service Statistics': live counts for one service, driven
        by the same service_id relationships every module already reads —
        no separate/duplicated per-module service list to keep in sync."""
        if user.get("role") not in READ_ROLES.get("services", {"admin"}):
            raise HTTPException(status_code=403, detail="Your role is not permitted to view service statistics")
        service = await db["erp_services"].find_one({"id": service_id}, {"_id": 0})
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        bookings = await db["erp_bookings"].find({"service_id": service_id}, {"_id": 0}).to_list(5000)
        invoices = await db["erp_invoices"].find({"service_id": service_id}, {"_id": 0}).to_list(5000)
        direct_customers = await db["erp_customers"].count_documents({"service_id": service_id})
        booked_customer_ids = {b.get("customer_id") for b in bookings if b.get("customer_id")}
        total_customers = len(booked_customer_ids) if booked_customer_ids or not direct_customers else max(direct_customers, len(booked_customer_ids))
        status_counts = {}
        for b in bookings:
            st = b.get("status") or "Unknown"
            status_counts[st] = status_counts.get(st, 0) + 1
        revenue = round(sum(float(i.get("total") or 0) for i in invoices), 2)
        return _ok({
            "service_id": service_id,
            "customers": total_customers,
            "bookings": len(bookings),
            "invoices": len(invoices),
            "revenue": revenue,
            "status_breakdown": status_counts,
        })

    @router.get("/admin/dashboard")
    async def admin_dashboard(user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        async def count(name, query=None):
            coll = COLLECTIONS[name][0]
            return await db[coll].count_documents(query or {})
        customers = await count("customers")
        employees = await count("employees")
        agents = await count("agents", {"status": "Active"})
        projects = await db["erp_projects"].find({}, {"_id": 0}).to_list(5000)
        invoices = await db["erp_invoices"].find({}, {"_id": 0}).to_list(5000)
        bookings = await db["erp_bookings"].find({}, {"_id": 0}).to_list(5000)
        compliance = []
        for n in ("gst", "itr", "tds", "roc"):
            compliance.extend(await db[COLLECTIONS[n][0]].find({}, {"_id": 0}).to_list(5000))
        payments = await db["erp_payments"].find({"status": {"$in": ["Completed", "Paid"]}}, {"_id": 0, "invoice_no": 1, "total": 1, "amount": 1}).to_list(10000)
        paid_by_invoice = {}
        for p in payments:
            if p.get("invoice_no"):
                paid_by_invoice[p["invoice_no"]] = paid_by_invoice.get(p["invoice_no"], 0) + float(p.get("total") or p.get("amount") or 0)
        revenue = 0
        outstanding = 0
        for inv in invoices:
            total = float(inv.get("total") or 0)
            paid = paid_by_invoice.get(inv.get("invoice_no") or inv.get("id"), 0)
            if paid <= 0 and inv.get("payment_status") == "Paid":
                paid = total
            paid = min(max(paid, 0), total)
            revenue += paid
            outstanding += max(total - paid, 0)
        revenue_monthly = {}
        customer_monthly = {}
        filing_monthly = {}
        for inv in invoices:
            m = str(inv.get("invoice_date") or inv.get("issue_date") or "")[:7]
            if m:
                inv_total = float(inv.get("total") or 0)
                inv_paid = paid_by_invoice.get(inv.get("invoice_no") or inv.get("id"), 0)
                if inv_paid <= 0 and inv.get("payment_status") == "Paid":
                    inv_paid = inv_total
                revenue_monthly[m] = revenue_monthly.get(m, 0) + min(max(inv_paid, 0), inv_total)
        customer_rows = await db["erp_customers"].find({}, {"_id": 0, "created_at": 1}).to_list(5000)
        for cdoc in customer_rows:
            m = str(cdoc.get("created_at") or "")[:7]
            if m: customer_monthly[m] = customer_monthly.get(m, 0) + 1
        for n in ("gst", "itr"):
            for row in await db[COLLECTIONS[n][0]].find({}, {"_id": 0}).to_list(5000):
                m = str(row.get("period") or row.get("filed_date") or row.get("created_at") or "")[:7]
                if m: filing_monthly.setdefault(m, {"gst": 0, "itr": 0})[n] += 1
        emp_rows = await db["erp_employees"].find({}, {"_id": 0, "name": 1, "performance": 1}).to_list(5000)
        performance = [{"name": e.get("name"), "score": float(e.get("performance") or 0)} for e in emp_rows if e.get("name")]
        return _ok({
            "cards": {"customers": customers, "employees": employees, "active_agents": agents,
                      "running_projects": sum(1 for p in projects if p.get("status") == "Running"),
                      "completed_projects": sum(1 for p in projects if p.get("status") == "Completed"),
                      "pending_projects": sum(1 for p in projects if p.get("status") == "Pending"),
                      "revenue": revenue, "outstanding": outstanding,
                      "paid_invoices": sum(1 for i in invoices if i.get("payment_status") == "Paid"),
                      "pending_invoices": sum(1 for i in invoices if i.get("payment_status") not in ("Paid", "Cancelled")),
                      "bookings": len(bookings), "compliance_open": sum(1 for c in compliance if c.get("status") not in ("Completed", "Filed", "Approved"))},
            "projects": projects, "invoices": invoices, "bookings": bookings, "compliance": compliance,
            "charts": {
                "revenue_monthly": [{"m": k, "revenue": v} for k,v in sorted(revenue_monthly.items())],
                "customer_growth": [{"m": k, "customers": v} for k,v in sorted(customer_monthly.items())],
                "filing_trend": [{"m": k, **v} for k,v in sorted(filing_monthly.items())],
                "performance": performance,
            }},
            message="Dashboard loaded")

    @router.get("/customer/dashboard")
    async def customer_dashboard(user: dict = Depends(get_current_user)):
        if user.get("role") != "customer":
            raise HTTPException(status_code=403, detail="Only customers can access this endpoint")
        customer_profile = await _customer_record_for_user(user)
        cid = customer_profile.get("id") if customer_profile else None
        if not cid:
            raise HTTPException(status_code=404, detail="No customer profile is linked to this account")

        bookings = await db["erp_bookings"].find({"customer_id": cid}, {"_id": 0}).to_list(2000)
        invoices = await db["erp_invoices"].find({"customer_id": cid}, {"_id": 0}).to_list(2000)
        documents = await db["erp_documents"].find({"customer_id": cid}, {"_id": 0}).to_list(2000)
        tickets = await db["erp_tickets"].find({"customer_id": cid}, {"_id": 0}).to_list(2000)
        notifications = await db["erp_notifications"].find({"$or": [{"user_id": cid}, {"role": "all"}]}, {"_id": 0}).to_list(500)

        completed = [b for b in bookings if b.get("status") == "Completed"]
        active = [b for b in bookings if b.get("status") in ("Pending", "Running", "Confirmed")]
        paid_invoices = []
        pending_invoices = []
        outstanding = 0
        for inv in invoices:
            state = await _invoice_financial_state(inv)
            inv.update({"paid_amount": state["paid_amount"], "balance": state["balance"], "payment_status": state["payment_status"]})
            if state["payment_status"] == "Paid":
                paid_invoices.append(inv)
            elif state["payment_status"] != "Cancelled":
                pending_invoices.append(inv)
                outstanding += state["balance"]
        payment_pending_bookings = [b for b in bookings if b.get("payment_status") != "Paid"]
        filings = []
        for n in ("gst", "itr", "tds", "roc"):
            rows = await db[COLLECTIONS[n][0]].find({"customer_id": cid}, {"_id": 0}).to_list(500)
            filings.extend(rows)
        spending = {}
        for i in invoices:
            month = str(i.get("invoice_date") or i.get("issue_date") or "")[:7]
            if month:
                spending[month] = spending.get(month, 0) + float(i.get("total") or 0)
        service_usage = {}
        for b in bookings:
            service_usage[b.get("service") or "Other"] = service_usage.get(b.get("service") or "Other", 0) + 1

        return _ok({
            "cards": {
                "total_services": len(bookings), "active_services": len(active),
                "completed_services": len(completed), "pending_payment": len(payment_pending_bookings),
                "outstanding": outstanding, "payments_done": len(paid_invoices),
                "pending_invoices": len(pending_invoices), "documents": len(documents),
                "notifications": len([n for n in notifications if not n.get("read")]),
                "tickets": len([t for t in tickets if t.get("status") not in ("Closed", "Resolved")]),
            },
            "filings": filings,
            "spending": [{"m": k, "amount": v} for k, v in sorted(spending.items())],
            "service_usage": [{"name": k, "value": v} for k, v in service_usage.items()],
            "due_dates": [{"title": b.get("service") or "Service", "date": b.get("due_date"), "type": "Service"} for b in bookings if b.get("due_date")],
        })

    @router.get("/reminders")
    async def reminders(user: dict = Depends(get_current_user)):
        from datetime import date
        today = date.today()
        out = []
        def add(title, company, due, kind, assigned):
            if not due:
                return
            try:
                d = date.fromisoformat(due)
            except Exception:
                return
            days = (d - today).days
            if days < 0:
                prio, ntype = "Overdue", "urgent"
            elif days <= 3:
                prio, ntype = "High", "warning"
            elif days <= 10:
                prio, ntype = "Medium", "information"
            else:
                prio, ntype = "Low", "information"
            out.append({"title": title, "company": company, "due_date": due, "days_remaining": days, "priority": prio, "type": ntype, "kind": kind, "assigned": assigned})

        for r in await db["erp_gst"].find({"status": {"$ne": "Completed"}}, {"_id": 0}).to_list(100):
            add(f"GST {r.get('return_type')} due", r.get("client"), r.get("due_date"), "GST", r.get("consultant"))
        for r in await db["erp_itr"].find({"status": {"$ne": "Completed"}}, {"_id": 0}).to_list(100):
            add("Income Tax return due", r.get("client"), r.get("due_date"), "Income Tax", r.get("consultant"))
        for r in await db["erp_tds"].find({"status": {"$ne": "Completed"}}, {"_id": 0}).to_list(100):
            add(f"TDS {r.get('form')} {r.get('quarter')} due", r.get("client"), r.get("due_date"), "TDS", "-")
        for r in await db["erp_roc"].find({"status": {"$ne": "Completed"}}, {"_id": 0}).to_list(100):
            add(f"ROC {r.get('form')} due", r.get("company"), r.get("due_date"), "ROC", r.get("consultant"))
        for r in await db["erp_invoices"].find({"payment_status": {"$ne": "Paid"}}, {"_id": 0}).to_list(100):
            add(f"Invoice {r.get('invoice_no')} payment due", r.get("customer"), r.get("invoice_date"), "Invoice", "-")

        out.sort(key=lambda x: x["days_remaining"])
        summary = {
            "today": len([x for x in out if x["days_remaining"] == 0]),
            "this_week": len([x for x in out if 0 <= x["days_remaining"] <= 7]),
            "overdue": len([x for x in out if x["days_remaining"] < 0]),
            "upcoming": len(out),
        }
        return _ok({"reminders": out, "summary": summary})

    @router.get("/accounting/summary")
    async def accounting_summary(user: dict = Depends(get_current_user)):
        income = 300500
        expenses = 425000
        return _ok({
            "income": income, "expenses": expenses, "profit": income - expenses,
            "cash_flow": [
                {"m": "Apr", "in": 610000, "out": 480000}, {"m": "May", "in": 540000, "out": 460000},
                {"m": "Jun", "in": 720000, "out": 505000}, {"m": "Jul", "in": 680000, "out": 425000},
            ],
            "pnl": [
                {"account": "Service Revenue", "amount": 1560000, "type": "Income"},
                {"account": "Consultancy Revenue", "amount": 640000, "type": "Income"},
                {"account": "Salaries", "amount": 380000, "type": "Expense"},
                {"account": "Office Rent", "amount": 45000, "type": "Expense"},
                {"account": "Software & Tools", "amount": 28000, "type": "Expense"},
                {"account": "Marketing", "amount": 62000, "type": "Expense"},
            ],
            "balance_sheet": [
                {"item": "Cash & Bank", "amount": 1240000, "type": "Asset"},
                {"item": "Accounts Receivable", "amount": 685000, "type": "Asset"},
                {"item": "Fixed Assets", "amount": 520000, "type": "Asset"},
                {"item": "Accounts Payable", "amount": 210000, "type": "Liability"},
                {"item": "Loans", "amount": 300000, "type": "Liability"},
                {"item": "Owner's Equity", "amount": 1935000, "type": "Equity"},
            ],
            "trial_balance": [
                {"account": "Bank", "debit": 1240000, "credit": 0},
                {"account": "Accounts Receivable", "debit": 685000, "credit": 0},
                {"account": "Service Revenue", "debit": 0, "credit": 1560000},
                {"account": "Consultancy Revenue", "debit": 0, "credit": 640000},
                {"account": "Salaries", "debit": 380000, "credit": 0},
                {"account": "Office Rent", "debit": 45000, "credit": 0},
            ],
        })

    @router.get("/notifications")
    async def list_notifications(search: str = Query(None), user: dict = Depends(get_current_user)):
        role = user.get("role")
        q = {"$or": [{"user_id": user.get("id")}, {"role": role, "user_id": {"$exists": False}}, {"role": "all"}]}
        try:
            items = await db["erp_notifications"].find(q, {"_id": 0}).sort("ts", -1).to_list(500)
            if search:
                s = search.lower()
                items = [i for i in items if s in (i.get("title", "") + i.get("description", "") + i.get("category", "")).lower()]
            unread = len([i for i in items if not i.get("read")])
            return _ok({"notifications": items, "unread": unread})
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Notification service cannot reach MongoDB. Check MONGO_URL/DB_NAME and make sure MongoDB is running/reachable.") from exc

    @router.post("/notifications/{nid}/read")
    async def mark_read(nid: str, user: dict = Depends(get_current_user)):
        try:
            res = await db["erp_notifications"].update_one({"id": nid, "$or": [{"user_id": user.get("id")}, {"role": user.get("role"), "user_id": {"$exists": False}}, {"role": "all"}]}, {"$set": {"read": True}})
            if not res.matched_count:
                raise HTTPException(status_code=404, detail="Notification not found")
            return _ok({"id": nid}, message="Marked read")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Notification service cannot reach MongoDB. Check MONGO_URL/DB_NAME.") from exc

    @router.post("/notifications/read-all")
    async def mark_all_read(user: dict = Depends(get_current_user)):
        try:
            await db["erp_notifications"].update_many({"$or": [{"user_id": user.get("id")}, {"role": user.get("role"), "user_id": {"$exists": False}}, {"role": "all"}]}, {"$set": {"read": True}})
            return _ok({}, message="All marked read")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Notification service cannot reach MongoDB. Check MONGO_URL/DB_NAME.") from exc

    @router.delete("/notifications/{nid}")
    async def del_notification(nid: str, user: dict = Depends(get_current_user)):
        try:
            res = await db["erp_notifications"].delete_one({"id": nid, "$or": [{"user_id": user.get("id")}, {"role": user.get("role"), "user_id": {"$exists": False}}, {"role": "all"}]})
            if not res.deleted_count:
                raise HTTPException(status_code=404, detail="Notification not found")
            return _ok({"id": nid}, message="Deleted")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Notification service cannot reach MongoDB. Check MONGO_URL/DB_NAME.") from exc




    # ---------------- Phase 2B: linked invoice/payment detail + analytics ----------------
    async def _invoice_paid_amount(invoice: dict):
        invoice_no = invoice.get("invoice_no") or invoice.get("id")
        rows = await db["erp_payments"].find(
            {"$or": [{"invoice_id": invoice.get("id")}, {"invoice_no": invoice_no}], "status": {"$in": ["Completed", "Paid"]}},
            {"_id": 0}
        ).to_list(5000)
        paid = sum(float(r.get("total") or r.get("amount") or 0) for r in rows)
        if paid <= 0 and invoice.get("payment_status") == "Paid":
            paid = float(invoice.get("total") or 0)
        return round(paid, 2), rows

    async def _invoice_financial_state(invoice: dict):
        """Return backend-authoritative invoice paid/balance/payment-status values."""
        paid, payments = await _invoice_paid_amount(invoice)
        total = round(float(invoice.get("total") or 0), 2)
        balance = round(max(total - paid, 0), 2)
        current = str(invoice.get("payment_status") or invoice.get("status") or "Pending")
        if current == "Cancelled":
            status = current
        elif balance <= 0.009:
            status = "Paid"
        else:
            due_date = str(invoice.get("due_date") or "")[:10]
            today = _ist_today_str()
            if due_date and due_date < today:
                status = "Overdue"
            elif paid > 0:
                status = "Partial"
            else:
                status = "Pending"
        return {"paid_amount": round(paid, 2), "balance": balance, "payment_status": status, "payments": payments}

    async def _sync_invoice_financials(invoice_id: str):
        invoice = await db["erp_invoices"].find_one({"id": invoice_id}, {"_id": 0})
        if not invoice:
            return None
        state = await _invoice_financial_state(invoice)
        await db["erp_invoices"].update_one(
            {"id": invoice_id},
            {"$set": {
                "paid_amount": state["paid_amount"],
                "balance": state["balance"],
                "payment_status": state["payment_status"],
            }},
        )
        invoice.update({k: state[k] for k in ("paid_amount", "balance", "payment_status")})
        return invoice

    async def _sync_booking_payment_status(invoice: dict):
        booking_id = invoice.get("booking_id")
        if not booking_id:
            return
        balance = float(invoice.get("balance") or 0)
        total = float(invoice.get("total") or 0)
        if balance <= 0.009:
            status = "Paid"
        elif total > 0 and balance < total:
            status = "Partial"
        else:
            status = "Pending"
        await db["erp_bookings"].update_one({"id": booking_id}, {"$set": {"payment_status": status}})

    async def _resolve_service(invoice: dict, booking: dict = None):
        service_id = invoice.get("service_id") or (booking or {}).get("service_id")
        service_name = invoice.get("service") or (booking or {}).get("service") or (booking or {}).get("service_name")
        service = None
        if service_id:
            service = await db["erp_services"].find_one({"id": service_id}, {"_id": 0})
        if not service and service_name:
            service = await db["erp_services"].find_one({"name": service_name}, {"_id": 0})
        return service, service_id, service_name

    async def _relationship_authorized(entity: str, record: dict, user: dict):
        role = user.get("role")
        if role == "admin":
            return True
        if role == "customer":
            customer = await _customer_record_for_user(user)
            cid = customer.get("id") if customer else None
            if entity == "customer":
                return bool(cid and record.get("id") == cid)
            return bool(cid and record.get("customer_id") == cid)
        if role == "agent":
            agent_id = (user.get("meta") or {}).get("agent_id")
            if not agent_id:
                agent = await _resolve_agent_id(name=user.get("name"))
                agent_id = agent
            if entity == "agent":
                return bool(agent_id and record.get("id") == agent_id)
            if entity == "booking":
                return record.get("agent_id") == agent_id
            if entity == "payment":
                return record.get("agent_id") == agent_id
        return role in {"employee"} and entity in {"booking", "invoice", "payment"}

    @router.get("/relationships/{entity}/{record_id}")
    async def relationship_details(entity: str, record_id: str, user: dict = Depends(get_current_user)):
        """Phase-4 cross-module relationship graph using stable backend IDs."""
        aliases = {
            "customer": ("erp_customers", "customer"),
            "booking": ("erp_bookings", "booking"),
            "service": ("erp_services", "service"),
            "agent": ("erp_agents", "agent"),
            "invoice": ("erp_invoices", "invoice"),
            "payment": ("erp_payments", "payment"),
        }
        if entity not in aliases:
            raise HTTPException(status_code=404, detail="Unknown relationship entity")
        coll, _ = aliases[entity]
        record = await db[coll].find_one({"id": record_id}, {"_id": 0})
        if not record:
            raise HTTPException(status_code=404, detail=f"{entity.title()} not found")
        if not await _relationship_authorized(entity, record, user):
            raise HTTPException(status_code=403, detail="You are not permitted to view these relationships")

        result = {"entity": entity, "record": record, "customer": None, "booking": None,
                  "service": None, "agent": None, "invoice": None, "payments": [],
                  "bookings": [], "invoices": [], "services": [], "agents": [], "customers": []}

        async def find_one(collection, key, value):
            if not value:
                return None
            return await db[collection].find_one({key: value}, {"_id": 0})

        if entity == "customer":
            result["bookings"] = await db["erp_bookings"].find({"customer_id": record["id"]}, {"_id": 0}).to_list(500)
            result["invoices"] = await db["erp_invoices"].find({"customer_id": record["id"]}, {"_id": 0}).to_list(500)
            result["payments"] = await db["erp_payments"].find({"customer_id": record["id"]}, {"_id": 0}).to_list(1000)
            # "Get Services by Customer": a customer can be linked to more than one
            # service — its primary service_id plus every distinct service_id used
            # across its own bookings. Dedup and resolve to full service records so
            # the Admin/Customer/Employee/Agent views can all render the same list.
            service_ids = {b.get("service_id") for b in result["bookings"] if b.get("service_id")}
            if record.get("service_id"):
                service_ids.add(record["service_id"])
            if service_ids:
                result["services"] = await db["erp_services"].find({"id": {"$in": list(service_ids)}}, {"_id": 0}).to_list(500)
        elif entity == "booking":
            result["customer"] = await find_one("erp_customers", "id", record.get("customer_id"))
            result["service"] = await find_one("erp_services", "id", record.get("service_id"))
            result["agent"] = await find_one("erp_agents", "id", record.get("agent_id"))
            result["invoice"] = await find_one("erp_invoices", "booking_id", record.get("id"))
            result["payments"] = await db["erp_payments"].find({"booking_id": record.get("id")}, {"_id": 0}).to_list(1000)
        elif entity == "service":
            result["bookings"] = await db["erp_bookings"].find({"service_id": record["id"]}, {"_id": 0}).to_list(500)
            result["invoices"] = await db["erp_invoices"].find({"service_id": record["id"]}, {"_id": 0}).to_list(500)
            result["payments"] = await db["erp_payments"].find({"service_id": record["id"]}, {"_id": 0}).to_list(1000)
            # "Get Customers by Service": every service has its own customer list —
            # customers assigned this service directly (customer.service_id) plus
            # customers who booked this service (erp_bookings.service_id), deduped.
            customer_ids = {b.get("customer_id") for b in result["bookings"] if b.get("customer_id")}
            direct_customers = await db["erp_customers"].find({"service_id": record["id"]}, {"_id": 0}).to_list(2000)
            customer_ids.update(c["id"] for c in direct_customers if c.get("id"))
            if customer_ids:
                result["customers"] = await db["erp_customers"].find({"id": {"$in": list(customer_ids)}}, {"_id": 0}).to_list(2000)
        elif entity == "agent":
            result["bookings"] = await db["erp_bookings"].find({"agent_id": record["id"]}, {"_id": 0}).to_list(500)
            result["payments"] = await db["erp_payments"].find({"agent_id": record["id"]}, {"_id": 0}).to_list(1000)
        elif entity == "invoice":
            result["customer"] = await find_one("erp_customers", "id", record.get("customer_id"))
            result["booking"] = await find_one("erp_bookings", "id", record.get("booking_id"))
            result["service"] = await find_one("erp_services", "id", record.get("service_id"))
            result["payments"] = await db["erp_payments"].find(
                {"$or": [{"invoice_id": record.get("id")}, {"invoice_no": record.get("invoice_no")}]},
                {"_id": 0}
            ).to_list(1000)
        elif entity == "payment":
            result["customer"] = await find_one("erp_customers", "id", record.get("customer_id"))
            result["booking"] = await find_one("erp_bookings", "id", record.get("booking_id"))
            result["service"] = await find_one("erp_services", "id", record.get("service_id"))
            result["invoice"] = await find_one("erp_invoices", "id", record.get("invoice_id"))
            if not result["invoice"] and record.get("invoice_no"):
                result["invoice"] = await find_one("erp_invoices", "invoice_no", record.get("invoice_no"))

        return _ok(result, message="Relationships loaded")

    @router.get("/admin/invoices/{invoice_id}/details")
    async def invoice_details(invoice_id: str, user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        invoice = await db["erp_invoices"].find_one({"id": invoice_id}, {"_id": 0})
        if not invoice:
            invoice = await db["erp_invoices"].find_one({"invoice_no": invoice_id}, {"_id": 0})
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        customer = None
        if invoice.get("customer_id"):
            customer = await db["erp_customers"].find_one({"id": invoice["customer_id"]}, {"_id": 0})
        booking = None
        if invoice.get("booking_id"):
            booking = await db["erp_bookings"].find_one({"id": invoice["booking_id"]}, {"_id": 0})
        service, service_id, service_name = await _resolve_service(invoice, booking)
        paid, payments = await _invoice_paid_amount(invoice)
        total = float(invoice.get("total") or 0)
        return _ok({
            "invoice": invoice,
            "customer": customer,
            "booking": booking,
            "service": service,
            "service_id": service_id,
            "service_name": service_name,
            "payments": payments,
            "financials": {"total": round(total, 2), "paid": paid, "balance": round(max(total - paid, 0), 2)},
        }, message="Invoice details loaded")

    @router.get("/admin/payments/{payment_id}/details")
    async def payment_details(payment_id: str, user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        payment = await db["erp_payments"].find_one({"id": payment_id}, {"_id": 0})
        if not payment:
            payment = await db["erp_payments"].find_one({"payment_id": payment_id}, {"_id": 0})
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        customer = None
        if payment.get("customer_id"):
            customer = await db["erp_customers"].find_one({"id": payment["customer_id"]}, {"_id": 0})
        invoice = None
        if payment.get("invoice_id"):
            invoice = await db["erp_invoices"].find_one({"id": payment["invoice_id"]}, {"_id": 0})
        if not invoice and payment.get("invoice_no"):
            invoice = await db["erp_invoices"].find_one({"invoice_no": payment["invoice_no"]}, {"_id": 0})
        booking = None
        if payment.get("booking_id"):
            booking = await db["erp_bookings"].find_one({"id": payment["booking_id"]}, {"_id": 0})
        service, service_id, service_name = await _resolve_service(invoice or {}, booking)
        financials = None
        if invoice:
            state = await _invoice_financial_state(invoice)
            financials = {"total": round(float(invoice.get("total") or 0), 2), "paid": state["paid_amount"], "balance": state["balance"], "payment_status": state["payment_status"]}
        return _ok({
            "payment": payment, "customer": customer, "invoice": invoice, "booking": booking,
            "service": service, "service_id": service_id, "service_name": service_name,
            "financials": financials,
        }, message="Payment details loaded")

    async def _agent_earnings_summary(agent_name: str, agent_id: str = None):
        if agent_id:
            commission_query = {"$or": [
                {"agent_id": agent_id},
                {"agent_id": {"$exists": False}, "agent": agent_name},
                {"agent_id": {"$exists": False}, "agent_name": agent_name},
            ]}
            payment_query = {"payment_type": "Agent Commission", "status": {"$in": ["Completed", "Paid"]},
                             "$or": [
                                 {"agent_id": agent_id},
                                 {"agent_id": {"$exists": False}, "agent": agent_name},
                             ]}
        else:
            commission_query = {"$or": [{"agent": agent_name}, {"agent_name": agent_name}]}
            payment_query = {"payment_type": "Agent Commission", "status": {"$in": ["Completed", "Paid"]},
                             "$or": [{"agent": agent_name}, {"agent_name": agent_name}]}
        commissions = await db["erp_commissions"].find(commission_query, {"_id": 0}).to_list(5000)
        earned = round(sum(float(c.get("amount") or 0) for c in commissions), 2)
        payments = await db["erp_payments"].find(
            payment_query,
            {"_id": 0}
        ).to_list(5000)
        paid_records = round(sum(float(p.get("total") or p.get("amount") or 0) for p in payments), 2)
        paid = paid_records
        return {
            "agent": agent_name,
            "total_earned": earned,
            "total_paid": paid,
            "due_amount": round(max(earned - paid, 0), 2),
            "commissions": commissions,
            "payments": payments,
        }

    @router.get("/agent/earnings-summary")
    async def agent_earnings_summary(user: dict = Depends(get_current_user)):
        if user.get("role") == "agent":
            agent_name = user.get("name")
            agent_id = (user.get("meta") or {}).get("agent_id")
            if agent_id:
                agent = await db["erp_agents"].find_one({"id": agent_id}, {"_id": 0, "name": 1})
                if agent and agent.get("name"):
                    agent_name = agent["name"]
        elif user.get("role") == "admin":
            agent_name = None
        else:
            raise HTTPException(status_code=403, detail="Agent or admin access required")
        if not agent_name:
            agents = await db["erp_agents"].find({}, {"_id": 0, "name": 1, "id": 1}).to_list(500)
            return _ok({"agents": [await _agent_earnings_summary(a.get("name"), a.get("id")) for a in agents if a.get("name")]}, message="Agent earnings loaded")
        return _ok(await _agent_earnings_summary(agent_name), message="Agent earnings loaded")

    async def _task_payable_amount(task: dict):
        for key in ("payable_amount", "agent_payable", "commission_amount", "agent_amount", "amount", "fee", "estimated_fee"):
            try:
                value = float(task.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return round(value, 2)
        booking_id = task.get("booking_id")
        if booking_id:
            booking = await db["erp_bookings"].find_one({"id": str(booking_id)}, {"_id": 0, "estimated_fee": 1})
            if booking:
                try:
                    return round(float(booking.get("estimated_fee") or 0), 2)
                except (TypeError, ValueError):
                    pass
        return 0.0

    @router.get("/admin/agent-tasks/payable")
    async def list_payable_agent_tasks(user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        tasks = await db["erp_tasks"].find({"status": {"$in": ["Completed", "completed", "Done", "done"]}}, {"_id": 0}).to_list(5000)
        result = []
        for task in tasks:
            agent_id = task.get("agent_id")
            agent_name = task.get("agent") or task.get("assigned_agent") or task.get("agent_name")
            if not agent_id and agent_name:
                agent_id = await _resolve_agent_id(name=agent_name)
            if not agent_id:
                continue
            amount = await _task_payable_amount(task)
            if amount <= 0:
                continue
            paid = await db["erp_payments"].find_one({"payment_type": "Agent Commission", "task_id": str(task.get("id"))}, {"_id": 0, "id": 1})
            if paid:
                continue
            result.append({
                "id": task.get("id"), "task_id": task.get("task_id") or task.get("id"),
                "title": task.get("title") or task.get("task_name") or task.get("name") or task.get("description") or task.get("id"),
                "agent_id": agent_id, "agent": agent_name, "booking_id": task.get("booking_id"),
                "service_id": task.get("service_id"), "completed_date": task.get("completed_date") or task.get("completed_at") or task.get("updated_at"),
                "payable_amount": amount, "status": task.get("status"),
            })
        return _ok(result, message="Completed payable agent tasks loaded")

    @router.post("/admin/agent-payments")
    async def record_agent_payment(body: dict = Body(...), user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        task_id = str(body.get("task_id") or "").strip()
        task = None
        if task_id:
            task = await db["erp_tasks"].find_one({"id": task_id}, {"_id": 0})
            if not task:
                task = await db["erp_tasks"].find_one({"task_id": task_id}, {"_id": 0})
            if not task or str(task.get("status", "")).lower() not in {"completed", "done"}:
                raise HTTPException(status_code=409, detail="Only completed tasks can become payable")
            if await db["erp_payments"].find_one({"payment_type": "Agent Commission", "task_id": str(task.get("id"))}, {"_id": 0}):
                raise HTTPException(status_code=409, detail="This completed task has already been paid")
        agent_id = str(body.get("agent_id") or (task or {}).get("agent_id") or "").strip()
        agent = str(body.get("agent") or (task or {}).get("agent") or (task or {}).get("assigned_agent") or (task or {}).get("agent_name") or "").strip()
        if agent_id:
            agent_doc = await db["erp_agents"].find_one({"id": agent_id}, {"_id": 0})
            if not agent_doc:
                raise HTTPException(status_code=404, detail="Agent not found")
            agent = agent_doc.get("name") or agent
        elif agent:
            agent_id = await _resolve_agent_id(name=agent)
            if not agent_id:
                raise HTTPException(status_code=422, detail="A valid agent_id is required")
        else:
            raise HTTPException(status_code=422, detail="Agent is required")
        task_amount = await _task_payable_amount(task) if task else 0
        amount = round(float(body.get("amount") or body.get("total") or task_amount), 2)
        if task and task_amount <= 0:
            raise HTTPException(status_code=422, detail="Completed task has no payable amount configured")
        if task and abs(amount - task_amount) > 0.009:
            raise HTTPException(status_code=400, detail=f"Task payable amount is ₹{task_amount:.2f}; payment must match it")
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Payment amount must be greater than zero")
        summary = await _agent_earnings_summary(agent, agent_id)
        # A task can become the source of a commission exactly once. This
        # preserves the existing earnings/payment system instead of creating a
        # disconnected payment ledger.
        if task:
            existing_commission = await db["erp_commissions"].find_one({"task_id": str(task.get("id"))}, {"_id": 0})
            if not existing_commission:
                commission_id = f"COM-{uuid.uuid4().hex[:8].upper()}"
                await db["erp_commissions"].insert_one({
                    "id": commission_id, "commission_id": commission_id, "agent_id": agent_id,
                    "agent": agent, "amount": amount, "task_id": str(task.get("id")),
                    "booking_id": task.get("booking_id"), "service_id": task.get("service_id"),
                    "status": "Completed", "earned_date": task.get("completed_date") or task.get("completed_at") or _ist_today_str(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                summary = await _agent_earnings_summary(agent, agent_id)
        if amount > summary["due_amount"] + 0.009:
            raise HTTPException(status_code=400, detail=f"Payment exceeds agent due amount of ₹{summary['due_amount']:.2f}")
        reference = str(body.get("reference_no") or "").strip()
        if reference and await db["erp_payments"].find_one({"payment_type": "Agent Commission", "reference_no": reference}, {"_id": 0}):
            raise HTTPException(status_code=409, detail="This agent payment reference is already recorded")
        payment_id = str(body.get("payment_id") or "").strip() or f"APM-{uuid.uuid4().hex[:8].upper()}"
        if await db["erp_payments"].find_one({"$or": [{"id": payment_id}, {"payment_id": payment_id}]}, {"_id": 0}):
            raise HTTPException(status_code=409, detail="This payment ID already exists")
        date_str = str(body.get("txn_date") or _ist_today_str())[:10]
        doc = {
            "id": payment_id, "payment_id": payment_id, "payment_type": "Agent Commission",
            "agent": agent, "agent_id": agent_id, "task_id": str(task.get("id")) if task else None,
            "booking_id": (task or {}).get("booking_id"), "service_id": (task or {}).get("service_id"),
            "completed_date": (task or {}).get("completed_date") or (task or {}).get("completed_at") if task else None,
            "amount": amount, "total": amount, "mode": body.get("mode") or "Bank Transfer",
            "reference_no": reference, "txn_date": date_str, "status": "Completed", "payment_status": "Paid",
            "remarks": body.get("remarks") or ("Completed task payment" if task else "Agent commission payment"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await db["erp_payments"].insert_one(doc)
        except Exception:
            await db["erp_payments"].delete_one({"id": payment_id})
            raise HTTPException(status_code=500, detail="Agent payment could not be recorded")
        return _ok(doc, message="Agent payment recorded")

    @router.get("/admin/synchronization/summary")
    async def synchronization_summary(user: dict = Depends(get_current_user)):
        """Authoritative Phase-6 snapshot for integration verification and support."""
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        invoices = await db["erp_invoices"].find({}, {"_id": 0}).to_list(10000)
        for invoice in invoices:
            state = await _invoice_financial_state(invoice)
            invoice.update({"paid_amount": state["paid_amount"], "balance": state["balance"], "payment_status": state["payment_status"]})
        payments = await db["erp_payments"].find({}, {"_id": 0}).to_list(10000)
        bookings = await db["erp_bookings"].find({}, {"_id": 0}).to_list(10000)
        customers = await db["erp_customers"].find({}, {"_id": 0}).to_list(10000)
        agents = await db["erp_agents"].find({}, {"_id": 0}).to_list(10000)
        attendance = await db["erp_attendance"].find({}, {"_id": 0}).to_list(10000)
        service_counts = {}
        for row in bookings:
            key = row.get("service") or row.get("service_name") or "Other"
            service_counts[key] = service_counts.get(key, 0) + 1
        return _ok({
            "source_of_truth": {
                "customer_service": "erp_customers.service_id / service_type",
                "booking_status": "erp_bookings.status",
                "agent_account_status": "erp_agents.status",
                "agent_attendance": "erp_attendance.status (attendance records; never agent status)",
                "invoice_amount": "erp_invoices.total",
                "invoice_paid_amount": "sum of eligible erp_payments for the invoice",
                "invoice_balance": "erp_invoices.balance derived from invoice total minus eligible payments",
                "payment_status": "erp_payments.status",
                "agent_earnings": "sum of erp_commissions.amount by agent_id",
                "agent_payments": "erp_payments where payment_type = Agent Commission",
            },
            "counts": {
                "customers": len(customers), "bookings": len(bookings), "invoices": len(invoices),
                "payments": len(payments), "agents": len(agents), "attendance": len(attendance),
            },
            "financials": {
                "invoiced": round(sum(float(i.get("total") or 0) for i in invoices), 2),
                "paid": round(sum(float(i.get("paid_amount") or 0) for i in invoices), 2),
                "balance": round(sum(float(i.get("balance") or 0) for i in invoices), 2),
            },
            "service_counts": service_counts,
        }, message="System synchronization snapshot loaded")

    @router.get("/admin/analytics/summary")
    async def analytics_summary(user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        invoices = await db["erp_invoices"].find({}, {"_id": 0}).to_list(10000)
        payments = await db["erp_payments"].find({}, {"_id": 0}).to_list(10000)
        bookings = await db["erp_bookings"].find({}, {"_id": 0}).to_list(10000)
        service_counts = {}
        for row in invoices:
            key = row.get("service") or row.get("service_name") or "Other"
            service_counts[key] = service_counts.get(key, 0) + 1
        payment_status = {}
        for row in payments:
            key = row.get("status") or "Unknown"
            payment_status[key] = payment_status.get(key, 0) + 1
        booking_status = {}
        for row in bookings:
            key = row.get("status") or "Unknown"
            booking_status[key] = booking_status.get(key, 0) + 1
        total_invoiced = 0.0
        total_paid = 0.0
        total_balance = 0.0
        for invoice in invoices:
            state = await _invoice_financial_state(invoice)
            total_invoiced += float(invoice.get("total") or 0)
            total_paid += float(state["paid_amount"])
            total_balance += float(state["balance"])
        return _ok({
            "service_counts": service_counts,
            "payment_status_counts": payment_status,
            "booking_status_counts": booking_status,
            "invoice_financials": {"invoiced": round(total_invoiced, 2), "paid": round(total_paid, 2), "balance": round(total_balance, 2)},
            "collection_rate": round((total_paid / total_invoiced) * 100, 2) if total_invoiced else 0,
        }, message="Analytics summary loaded")

    # ---------------- Payments (modular Razorpay: real when keys present, else demo test-mock) ----------------
    def _rzp_client():
        kid = os.environ.get("RAZORPAY_KEY_ID"); ksec = os.environ.get("RAZORPAY_KEY_SECRET")
        if kid and ksec:
            try:
                import razorpay
                return razorpay.Client(auth=(kid, ksec)), kid
            except Exception:
                return None, None
        return None, None

    @router.post("/payments/create-order")
    async def create_order(body: dict = Body(...), user: dict = Depends(get_current_user)):
        amount_rupees = float(body.get("amount") or 0)
        if amount_rupees <= 0:
            raise HTTPException(status_code=400, detail="Invalid amount")
        amount_paise = int(round(amount_rupees * 100))
        client, kid = _rzp_client()
        if client:
            order = client.order.create({"amount": amount_paise, "currency": "INR", "payment_capture": 1})
            return _ok({"order_id": order["id"], "amount": amount_paise, "currency": "INR", "key_id": kid, "mode": "live"})
        order_id = f"order_{uuid.uuid4().hex[:14]}"
        return _ok({"order_id": order_id, "amount": amount_paise, "currency": "INR",
                    "key_id": os.environ.get("RAZORPAY_KEY_ID", "rzp_test_DEMO1234567890"), "mode": "test"})

    @router.post("/payments/verify")
    async def verify_payment(body: dict = Body(...), user: dict = Depends(get_current_user)):
        order_id = body.get("order_id")
        if not order_id:
            raise HTTPException(status_code=422, detail="order_id is required")
        payment_id = body.get("payment_id") or f"pay_{uuid.uuid4().hex[:14]}"
        signature = body.get("signature")
        booking_id = body.get("booking_id")
        invoice_id = body.get("invoice_id")
        fee = float(body.get("amount") or 0)
        gst = float(body.get("gst") or 0)
        agent = body.get("agent", "Vikram Singh")

        # Idempotency: if this order was already verified (customer refreshed the
        # page, double-clicked, or retried after a slow response), return the
        # original result instead of creating a second payment/invoice pair.
        existing_payment = await db["erp_payments"].find_one({"order_id": order_id}, {"_id": 0})
        if existing_payment:
            return _ok({
                "payment_id": existing_payment["payment_id"], "order_id": order_id,
                "reference_no": existing_payment["reference_no"], "invoice_no": existing_payment["invoice_no"],
                "receipt_no": existing_payment.get("receipt_no", ""), "amount": existing_payment["amount"],
                "gst": existing_payment["gst"], "total": existing_payment["total"],
                "status": existing_payment["status"], "date": existing_payment["txn_date"],
            }, message="Payment already verified")

        invoice = None
        if invoice_id:
            invoice = await db["erp_invoices"].find_one(
                {"$or": [{"id": str(invoice_id)}, {"invoice_no": str(invoice_id)}]},
                {"_id": 0},
            )
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if user.get("role") == "customer":
                customer_profile = await _customer_record_for_user(user)
                canonical_customer_id = customer_profile.get("id") if customer_profile else None
                if invoice.get("customer_id") != canonical_customer_id:
                    raise HTTPException(status_code=403, detail="You can only pay your own invoice")
            if invoice.get("payment_status") == "Cancelled":
                raise HTTPException(status_code=400, detail="Cancelled invoices cannot receive payments")
            state = await _invoice_financial_state(invoice)
            requested_total = round(fee + gst, 2)
            if requested_total <= 0:
                raise HTTPException(status_code=400, detail="Invalid payment amount")
            if requested_total > state["balance"] + 0.009:
                raise HTTPException(status_code=400, detail=f"Payment exceeds invoice balance of ₹{state['balance']:.2f}")
            if invoice.get("booking_id"):
                booking_id = invoice.get("booking_id")

        booking = None
        if booking_id:
            booking = await db["erp_bookings"].find_one({"id": booking_id}, {"_id": 0})
            if not booking:
                raise HTTPException(status_code=404, detail="Booking not found")
            # A customer can only ever pay for their own booking — never trust a
            # booking_id blindly, or another customer's pending booking could be
            # marked paid on their behalf.
            if user.get("role") == "customer":
                customer_profile = await _customer_record_for_user(user)
                canonical_customer_id = customer_profile.get("id") if customer_profile else None
                if booking.get("customer_id") != canonical_customer_id:
                    raise HTTPException(status_code=403, detail="You can only pay for your own booking")

        # The customer name shown on the invoice/payment record comes from the
        # authenticated user or the existing invoice/booking, never an unauthenticated
        # client-supplied relationship key.
        if invoice:
            customer = invoice.get("customer") or user.get("name") or "Customer"
            customer_id = invoice.get("customer_id")
        elif booking:
            customer = booking.get("customer") or user.get("name") or "Customer"
            customer_id = booking.get("customer_id")
        elif user.get("role") == "customer":
            customer_profile = await _customer_record_for_user(user)
            customer = (customer_profile or {}).get("business_name") or user.get("name") or "Customer"
            customer_id = customer_profile.get("id") if customer_profile else None
        else:
            customer = body.get("customer", "Customer")
            customer_id = None

        client, _ = _rzp_client()
        if client and signature:
            try:
                client.utility.verify_payment_signature({
                    "razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": signature,
                })
            except Exception:
                raise HTTPException(status_code=400, detail="Payment signature verification failed")

        now = datetime.now(timezone.utc)
        total = round(fee + gst)
        inv_no = f"INV-{3200 + int(now.timestamp()) % 800}"
        rcpt_no = f"RCPT-{uuid.uuid4().hex[:8].upper()}"
        txn_ref = f"TXN{int(now.timestamp())}"
        date_str = now.strftime("%Y-%m-%d")

        service_id = (invoice or booking or {}).get("service_id") if (invoice or booking) else None
        service_name = (invoice or booking or {}).get("service") if (invoice or booking) else body.get("service")
        agent_id = (invoice or booking or {}).get("agent_id") if (invoice or booking) else None
        target_invoice_id = invoice.get("id") if invoice else inv_no
        await db["erp_payments"].insert_one({
            "id": payment_id, "payment_id": payment_id, "order_id": order_id, "reference_no": txn_ref,
            "customer": customer, "customer_id": customer_id, "invoice_id": target_invoice_id,
            "invoice_no": invoice.get("invoice_no") if invoice else inv_no,
            "amount": round(fee), "gst": round(gst), "total": total,
            "mode": "Razorpay", "status": "Completed", "txn_date": date_str, "agent": agent,
            "agent_id": agent_id, "service": service_name, "service_id": service_id,
            "booking_id": booking_id, "receipt_no": rcpt_no,
            "remarks": "Invoice payment via Razorpay (test)" if invoice else "Booking payment via Razorpay (test)",
        })
        if not invoice:
            await db["erp_invoices"].insert_one({
                "id": inv_no, "invoice_no": inv_no, "customer": customer, "customer_id": customer_id,
                "booking_id": booking_id, "service": service_name, "service_id": service_id,
                "amount": round(fee), "gst": round(gst),
                "total": total, "paid_amount": total, "balance": 0, "status": "Paid", "payment_status": "Paid",
                "issue_date": date_str, "due_date": date_str,
            })
        else:
            synced_invoice = await _sync_invoice_financials(invoice["id"])
            await _sync_invoice_dependents(synced_invoice or invoice)
            await _sync_booking_payment_status(synced_invoice or invoice)
        if booking_id:
            await db["erp_bookings"].update_one({"id": booking_id}, {"$set": {"status": "Confirmed", "payment_status": "Paid"}})

        effective_invoice_no = invoice.get("invoice_no") if invoice else inv_no
        await _notify("admin", f"Payment received — {txn_ref}", f"{customer} paid ₹{total:,} for booking {booking_id or ''}. Invoice {effective_invoice_no}.", "Invoice", "information")
        await _notify("agent", f"Booking {booking_id or ''} paid & confirmed", f"{customer} completed payment of ₹{total:,}. You can begin the work.", "Invoice", "information")
        await _notify("customer", f"Payment successful — {inv_no}", f"Your payment of ₹{total:,} is confirmed. Booking {booking_id or ''} is now Confirmed.", "Invoice", "information")

        return _ok({
            "payment_id": payment_id, "order_id": order_id, "reference_no": txn_ref, "invoice_no": effective_invoice_no,
            "receipt_no": rcpt_no, "amount": round(fee), "gst": round(gst), "total": total,
            "status": "Completed", "date": date_str,
        }, message="Payment verified")

    # ---------------- Per-booking chat thread ----------------
    async def _authorize_booking_chat(booking_id: str, user: dict):
        booking = await db["erp_bookings"].find_one({"id": booking_id}, {"_id": 0})
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        role = user.get("role")
        if role == "admin":
            return booking
        if role == "customer":
            customer = await _customer_record_for_user(user)
            if customer and booking.get("customer_id") == customer.get("id"):
                return booking
        if role == "agent":
            agent_id = (user.get("meta") or {}).get("agent_id")
            if agent_id and booking.get("agent_id") == agent_id:
                return booking
        if role == "employee":
            emp = await _employee_record_for_user(user)
            if emp and booking.get("assigned_employee") == emp.get("name"):
                return booking
        raise HTTPException(status_code=404, detail="Booking not found")

    @router.get("/bookings/{booking_id}/messages")
    async def list_booking_messages(booking_id: str, user: dict = Depends(get_current_user)):
        await _authorize_booking_chat(booking_id, user)
        msgs = await db["erp_booking_chat"].find({"booking_id": booking_id}, {"_id": 0}).sort("ts", 1).to_list(500)
        return _ok({"messages": msgs})

    @router.post("/bookings/{booking_id}/messages")
    async def add_booking_message(booking_id: str, body: dict = Body(...), user: dict = Depends(get_current_user)):
        await _authorize_booking_chat(booking_id, user)
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Message is required")
        role = user.get("role", "customer")
        name = user.get("name") or {"customer": "Customer", "agent": "Consultant", "admin": "NTAXCO Admin", "employee": "NTAXCO Team"}.get(role, role.title())
        doc = {
            "id": f"MSG-{uuid.uuid4().hex[:8].upper()}", "booking_id": booking_id,
            "sender_role": role, "sender_name": name, "text": text,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        await db["erp_booking_chat"].insert_one({**doc})
        target = "agent" if role == "customer" else "customer"
        await _notify(target, f"New message on {booking_id}", f"{name}: {text[:60]}", "Bookings", "information")
        return _ok(doc, message="Message sent")


    @router.get("/employee/dashboard")
    async def employee_dashboard(user: dict = Depends(get_current_user)):
        if user.get("role") != "employee":
            raise HTTPException(status_code=403, detail="Employee access required")
        emp = await _employee_record_for_user(user)
        if not emp:
            raise HTTPException(status_code=404, detail="Employee profile not found")
        emp_name, emp_id = emp.get("name"), emp.get("id")
        customers = await db["erp_customers"].count_documents({"assigned_employee": emp_name})
        projects = await db["erp_projects"].find({"assigned_employee": emp_name}, {"_id": 0}).to_list(2000)
        tasks = await db["erp_tasks"].find({"employee_id": emp_id}, {"_id": 0}).to_list(2000)
        attendance = await db["erp_attendance"].find({"employee_id": emp_id}, {"_id": 0}).to_list(500)
        leaves = await db["erp_leaves"].find({"employee_id": emp_id}, {"_id": 0}).to_list(500)
        docs = await db["erp_documents"].find({"$or": [{"uploaded_by": emp_name}, {"assigned_employee": emp_name}]}, {"_id": 0}).to_list(2000)
        # Employee Module: "Assigned customer services" / "Employee service
        # workload" (section 5) — grouped from this employee's own assigned
        # customers, resolved against the live service catalog. No separate
        # per-employee service list is maintained; it's derived on read.
        assigned_customers = await db["erp_customers"].find({"assigned_employee": emp_name}, {"_id": 0}).to_list(2000)
        service_ids = {c.get("service_id") for c in assigned_customers if c.get("service_id")}
        services_by_id = {}
        if service_ids:
            for s in await db["erp_services"].find({"id": {"$in": list(service_ids)}}, {"_id": 0}).to_list(500):
                services_by_id[s["id"]] = s
        workload = {}
        for c in assigned_customers:
            label = (services_by_id.get(c.get("service_id")) or {}).get("name") or (services_by_id.get(c.get("service_id")) or {}).get("title") or c.get("service_type") or "Unassigned"
            workload[label] = workload.get(label, 0) + 1
        return _ok({
            "employee": emp, "cards": {
                "customers": customers, "projects": len(projects),
                "running_projects": sum(1 for p in projects if p.get("status") == "Running"),
                "completed_tasks": sum(1 for t in tasks if str(t.get("status","")).lower() in ("completed","done")),
                "pending_tasks": sum(1 for t in tasks if str(t.get("status","")).lower() not in ("completed","done")),
                "attendance_days": len(attendance), "pending_leaves": sum(1 for l in leaves if l.get("status") == "Pending"),
                "documents": len(docs),
            },
            "projects": projects, "tasks": tasks, "attendance": attendance[-31:], "leaves": leaves,
            "service_workload": [{"service": k, "customers": v} for k, v in sorted(workload.items(), key=lambda kv: -kv[1])],
        })

    @router.get("/agent/dashboard")
    async def agent_dashboard(user: dict = Depends(get_current_user)):
        if user.get("role") != "agent":
            raise HTTPException(status_code=403, detail="Agent access required")
        agent_id = (user.get("meta") or {}).get("agent_id")
        agent_doc = await db["erp_agents"].find_one({"id": agent_id}, {"_id": 0}) if agent_id else None
        agent_name = (agent_doc or {}).get("name") or user.get("name")
        leads = await db["erp_leads"].find({"$or": [{"assigned_agent": agent_name}, {"agent": agent_name}, {"agent_name": agent_name}]}, {"_id": 0}).to_list(2000)
        customers = await db["erp_customers"].count_documents({"assigned_agent": agent_name})
        bookings = await db["erp_bookings"].find(
            {"$or": [{"agent_id": agent_id}, {"assigned_agent": agent_name}]},
            {"_id": 0},
        ).to_list(2000)
        projects = await db["erp_projects"].find({"assigned_agent": agent_name}, {"_id": 0}).to_list(2000)
        commissions = await db["erp_commissions"].find({"agent": agent_name}, {"_id": 0}).to_list(2000)
        appointments = await db["erp_appointments"].find({"$or": [{"assigned_agent": agent_name}, {"agent": agent_name}]}, {"_id": 0}).to_list(2000)
        notifications = await db["erp_notifications"].count_documents({"$or": [{"role": "agent", "user_id": user.get("id")}, {"role": "agent", "user_id": {"$exists": False}}, {"role": "all"}]})
        monthly = {}
        for row in commissions:
            period = row.get("period") or str(row.get("created_at",""))[:7] or "Unknown"
            monthly[period] = monthly.get(period, 0) + float(row.get("amount") or 0)
        # Agent Module: "Agent service workload" (section 5) — grouped from
        # this agent's own bookings, resolved against the live service
        # catalog. Customer-specific Agent Percentage is untouched by this;
        # it's read straight off each customer record, never averaged/globalized.
        workload = {}
        for b in bookings:
            label = b.get("service") or "Unassigned"
            workload[label] = workload.get(label, 0) + 1
        return _ok({
            "cards": {
                "leads": len(leads), "new_leads": sum(1 for x in leads if str(x.get("status","")).lower() in ("new","open")),
                "qualified_leads": sum(1 for x in leads if str(x.get("status","")).lower() == "qualified"),
                "converted_customers": sum(1 for x in leads if str(x.get("status","")).lower() in ("converted","won")),
                "followups": sum(1 for x in leads if str(x.get("status","")).lower() in ("follow-up","followup")),
                "appointments": sum(1 for x in appointments if str(x.get("status","")).lower() not in ("completed","cancelled")),
                "customers": customers, "projects": len(projects), "bookings": len(bookings),
                "commission": sum(float(x.get("amount") or 0) for x in commissions), "notifications": notifications,
            },
            "leads": leads, "bookings": bookings, "projects": projects, "commissions": commissions,
            "appointments": appointments, "commission_trend": [{"period": k, "amount": v} for k,v in sorted(monthly.items())],
            "service_workload": [{"service": k, "bookings": v} for k, v in sorted(workload.items(), key=lambda kv: -kv[1])],
        })

    @router.post("/calculators/gst")
    async def gst_calculator(body: dict = Body(...), user: dict = Depends(get_current_user)):
        if user.get("role") != "customer":
            raise HTTPException(status_code=403, detail="Customer access required")
        try:
            amount = float(body.get("amount", 0)); rate = float(body.get("rate", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Amount and GST rate must be numbers")
        if amount < 0 or rate < 0 or rate > 100:
            raise HTTPException(status_code=422, detail="Enter a valid amount and GST rate")
        mode = str(body.get("mode", "exclusive")).lower()
        interstate = bool(body.get("interstate", False))
        if mode == "inclusive":
            base = round(amount / (1 + rate / 100), 2) if rate else round(amount, 2)
            gst = round(amount - base, 2)
            total = round(amount, 2)
        else:
            base = round(amount, 2); gst = round(base * rate / 100, 2); total = round(base + gst, 2)
        return _ok({"base_amount": base, "gst_amount": gst,
                    "cgst": 0 if interstate else round(gst/2,2),
                    "sgst": 0 if interstate else round(gst/2,2),
                    "igst": gst if interstate else 0, "final_amount": total,
                    "mode": mode, "rate": rate}, message="GST estimate calculated")

    @router.post("/calculators/income-tax")
    async def income_tax_calculator(body: dict = Body(...), user: dict = Depends(get_current_user)):
        if user.get("role") != "customer":
            raise HTTPException(status_code=403, detail="Customer access required")
        try:
            annual = max(0.0, float(body.get("annual_income", 0) or 0))
            deductions = max(0.0, float(body.get("deductions", 0) or 0))
            other = max(0.0, float(body.get("other_income", 0) or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Income and deductions must be numbers")
        regime = str(body.get("regime", "new")).lower()
        taxable = max(0.0, annual + other - deductions)
        if regime == "old":
            slabs = [(250000,0),(500000,.05),(1000000,.20),(float("inf"),.30)]
            tax = 0.0; prev=0.0
            for upper, rate in slabs:
                taxable_slice=max(0.0,min(taxable,upper)-prev); tax += taxable_slice*rate
                if taxable <= upper: break
                prev=upper
            if taxable <= 500000: tax=max(0.0,tax-12500)
        else:
            slabs = [(400000,0),(800000,.05),(1200000,.10),(1600000,.15),(2000000,.20),(2400000,.25),(float("inf"),.30)]
            tax=0.0; prev=0.0
            for upper, rate in slabs:
                taxable_slice=max(0.0,min(taxable,upper)-prev); tax += taxable_slice*rate
                if taxable <= upper: break
                prev=upper
            if taxable <= 1200000: tax=max(0.0,tax-60000)
        cess = round(tax * .04, 2)
        estimated = round(tax + cess, 2)
        return _ok({"gross_income": round(annual+other,2), "deductions": round(deductions,2),
                    "taxable_income": round(taxable,2), "income_tax": round(tax,2),
                    "cess": cess, "estimated_tax": estimated, "regime": regime,
                    "assessment_year": "2026-27"}, message="Income tax estimate calculated")
    return router


async def ensure_phase4_relationships(db):
    """Backfill and index stable Phase-4 foreign keys without changing display fields.

    Existing records are migrated once where an unambiguous match exists. Names
    are used only to locate legacy records during migration; all persisted
    relationships after this pass use backend IDs.
    """
    # Link customer portal accounts to their canonical ERP customer record.
    async for user in db.users.find({"role": "customer"}, {"_id": 0, "id": 1, "email": 1, "mobile": 1, "meta": 1}):
        if (user.get("meta") or {}).get("customer_id"):
            continue
        clauses = []
        if user.get("email"):
            clauses.append({"email": user["email"]})
        if user.get("mobile"):
            clauses.append({"mobile": user["mobile"]})
        if clauses:
            customer = await db["erp_customers"].find_one({"$or": clauses}, {"_id": 0, "id": 1})
            if customer:
                await db.users.update_one({"id": user["id"]}, {"$set": {"meta.customer_id": customer["id"]}})

    customers = await db["erp_customers"].find({}, {"_id": 0, "id": 1, "business_name": 1}).to_list(10000)
    customer_by_name = {str(x.get("business_name", "")).strip().lower(): x["id"] for x in customers if x.get("business_name")}

    services = await db["erp_services"].find({}, {"_id": 0, "id": 1, "name": 1, "title": 1}).to_list(10000)
    service_by_name = {}
    for x in services:
        for key in (x.get("name"), x.get("title")):
            if key:
                service_by_name[str(key).strip().lower()] = x["id"]

    agents = await db["erp_agents"].find({}, {"_id": 0, "id": 1, "name": 1}).to_list(10000)
    agent_by_name = {str(x.get("name", "")).strip().lower(): x["id"] for x in agents if x.get("name")}

    async def canonical_customer(value, display=None):
        if value:
            if any(x["id"] == value for x in customers):
                return value
            account = await db.users.find_one(
                {"role": "customer", "$or": [{"id": value}, {"meta.customer_id": value}]},
                {"_id": 0, "meta": 1},
            )
            cid = (account or {}).get("meta", {}).get("customer_id")
            if cid:
                return cid
        if display:
            return customer_by_name.get(str(display).strip().lower())
        return None

    async def canonical_service(value, display=None):
        if value and any(x["id"] == value for x in services):
            return value
        return service_by_name.get(str(display).strip().lower()) if display else None

    async def canonical_agent(value, display=None):
        if value and any(x["id"] == value for x in agents):
            return value
        return agent_by_name.get(str(display).strip().lower()) if display else None

    # Customer relationships.
    for collection in ("erp_bookings", "erp_invoices", "erp_payments", "erp_projects", "erp_documents", "erp_tickets", "erp_gst", "erp_itr", "erp_tds", "erp_roc"):
        async for row in db[collection].find({}, {"_id": 0, "id": 1, "customer_id": 1, "customer": 1, "client": 1, "client_name": 1, "company": 1, "business_name": 1}):
            cid = await canonical_customer(
                row.get("customer_id"),
                row.get("customer") or row.get("client") or row.get("client_name") or row.get("company") or row.get("business_name"),
            )
            if cid and row.get("customer_id") != cid:
                await db[collection].update_one({"id": row["id"]}, {"$set": {"customer_id": cid}})

    # Service relationships.
    for collection in ("erp_bookings", "erp_invoices", "erp_payments"):
        async for row in db[collection].find({}, {"_id": 0, "id": 1, "service_id": 1, "service": 1, "service_name": 1}):
            sid = await canonical_service(row.get("service_id"), row.get("service") or row.get("service_name"))
            if sid and row.get("service_id") != sid:
                await db[collection].update_one({"id": row["id"]}, {"$set": {"service_id": sid}})

    # Agent relationships.
    for collection in ("erp_bookings", "erp_commissions", "erp_payments", "erp_leads", "erp_appointments", "erp_projects", "erp_documents"):
        async for row in db[collection].find({}, {"_id": 0, "id": 1, "agent_id": 1, "assigned_agent": 1, "agent": 1, "agent_name": 1}):
            aid = await canonical_agent(row.get("agent_id"), row.get("assigned_agent") or row.get("agent") or row.get("agent_name"))
            if aid and row.get("agent_id") != aid:
                await db[collection].update_one({"id": row["id"]}, {"$set": {"agent_id": aid}})

    # Invoice relationships on payments.
    async for row in db["erp_payments"].find({}, {"_id": 0, "id": 1, "invoice_id": 1, "invoice_no": 1}):
        invoice = None
        if row.get("invoice_id"):
            invoice = await db["erp_invoices"].find_one({"id": row["invoice_id"]}, {"_id": 0, "id": 1})
        if not invoice and row.get("invoice_no"):
            invoice = await db["erp_invoices"].find_one({"invoice_no": row["invoice_no"]}, {"_id": 0, "id": 1, "invoice_no": 1})
        if invoice and row.get("invoice_id") != invoice["id"]:
            await db["erp_payments"].update_one({"id": row["id"]}, {"$set": {"invoice_id": invoice["id"]}})

    # Query indexes for relationship-heavy screens and financial reconciliation.
    indexes = [
        ("erp_bookings", [("customer_id", 1), ("service_id", 1), ("agent_id", 1)]),
        ("erp_invoices", [("customer_id", 1), ("booking_id", 1), ("service_id", 1)]),
        ("erp_payments", [("customer_id", 1), ("invoice_id", 1), ("booking_id", 1), ("service_id", 1), ("agent_id", 1)]),
        ("erp_commissions", [("agent_id", 1)]),
    ]
    for collection, fields in indexes:
        try:
            await db[collection].create_index(fields, name="phase4_relationships")
        except Exception:
            # Mongo permits equivalent indexes with different names; relationship
            # correctness must not prevent an otherwise healthy backend from booting.
            pass


async def seed_erp(db):
    """Optional development seed, disabled by default.

    Real ERP deployments must start with an empty/real MongoDB database and
    create records through the authenticated Admin/portal workflows. Existing
    MongoDB data is never deleted or overwritten by startup.
    """
    import os
    if os.getenv("NTAXCO_ENABLE_DEMO_SEED", "false").lower() != "true":
        return
    for name, (coll, seed, prefix) in COLLECTIONS.items():
        if await db[coll].count_documents({}) == 0 and seed:
            await db[coll].insert_many([{**d} for d in seed])
