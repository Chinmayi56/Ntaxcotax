import { useMemo, useState } from "react";
import { toast } from "sonner";
import api, { describeApiError } from "@/lib/api";
import { useCrud } from "@/hooks/useCrud";
import { inr } from "@/lib/utils";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StatusBadge } from "@/components/shared/StatusBadge";
import RelationshipPanel from "@/components/shared/RelationshipPanel";
import {
  Pencil, Upload, Download, Trash2, Loader2, FileText, ReceiptText, CalendarDays,
  CheckCircle2, Clock3, Landmark, FileCheck2, Building2, WalletCards, BriefcaseBusiness,
} from "lucide-react";

const DOC_TYPES = [
  "GST Certificate", "PAN Card", "TAN Certificate", "CIN Certificate",
  "Company Registration Certificate", "Address Proof", "Other Document",
];
const ALLOWED_DOC_MIMES = [
  "application/pdf", "image/jpeg", "image/png", "image/webp",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
];
const MAX_DOC_BYTES = 5 * 1024 * 1024;

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function SectionCard({ title, children, className = "", id }) {
  return (
    <div id={id} className={`rounded-xl border border-zinc-200 bg-white p-4 ${className}`}>
      <div className="text-xs font-bold uppercase tracking-wide text-blue-700 mb-3">{title}</div>
      {children}
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex flex-col gap-1 py-2.5 border-b border-zinc-100 last:border-b-0">
      <span className="text-[11px] text-zinc-500">{label}</span>
      <span className="text-sm font-medium text-zinc-800 break-words">{value ?? "—"}</span>
    </div>
  );
}

function ServiceIcon({ name }) {
  const n = String(name || "").toLowerCase();
  const Icon = n.includes("income") ? Landmark : n.includes("tds") ? FileCheck2 : n.includes("gst") ? FileText : n.includes("roc") ? Building2 : BriefcaseBusiness;
  return <Icon className="h-5 w-5" />;
}

export default function CustomerDetailTabs({ customer, onEdit, onRefresh, initialTab = "services" }) {
  const { rows: allBookings } = useCrud("bookings");
  const { rows: allInvoices } = useCrud("invoices");
  const { rows: allPayments } = useCrud("payments");
  const { rows: allDocuments, load: loadDocuments, remove: removeDocument } = useCrud("documents");

  const bookings = useMemo(() => allBookings.filter((b) => b.customer_id === customer.id), [allBookings, customer.id]);
  const invoices = useMemo(() => allInvoices.filter((i) => i.customer_id === customer.id), [allInvoices, customer.id]);
  const payments = useMemo(() => allPayments.filter((p) => p.customer_id === customer.id), [allPayments, customer.id]);
  const documents = useMemo(() => allDocuments.filter((d) => d.customer_id === customer.id), [allDocuments, customer.id]);

  const totals = useMemo(() => {
    const billed = invoices.reduce((s, i) => s + Number(i.total || 0), 0);
    const paid = invoices.reduce((s, i) => s + Number(i.paid_amount || 0), 0);
    const outstanding = invoices.reduce((s, i) => s + Number(i.balance ?? (Number(i.total || 0) - Number(i.paid_amount || 0))), 0);
    const pendingPayments = payments.filter((p) => ["Pending", "Failed"].includes(String(p.status || ""))).reduce((s, p) => s + Number(p.total || p.amount || 0), 0);
    return { billed, paid, outstanding, pendingPayments };
  }, [invoices, payments]);

  const [activeTab, setActiveTab] = useState(initialTab); const [docType, setDocType] = useState(DOC_TYPES[0]);
  const [uploading, setUploading] = useState(false);
  const [serviceFilter, setServiceFilter] = useState("All Services");

  const serviceRows = useMemo(() => {
    const values = [];
    if (customer.service_type) values.push({
      id: `customer-${customer.id}-service`,
      name: customer.service_type,
      status: customer.status,
      assigned_employee: customer.assigned_employee,
      start_date: customer.registration_date,
      due_date: null,
      progress: null,
      source: "Customer assignment",
    });
    bookings.forEach((b) => values.push({
      id: b.id, name: b.service || b.service_id || "Service",
      status: b.status, assigned_employee: b.assigned_employee || b.assigned_agent,
      start_date: b.booking_date || b.created_at, due_date: b.due_date,
      progress: b.progress, source: "Booking",
    }));
    return values;
  }, [customer, bookings]);

  const visibleServices = useMemo(() => serviceFilter === "All Services"
    ? serviceRows
    : serviceRows.filter((s) => String(s.name || "").toLowerCase().includes(serviceFilter.toLowerCase())), [serviceRows, serviceFilter]);

  const onDocFile = async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    if (!ALLOWED_DOC_MIMES.includes(f.type)) return toast.error("Supported types: PDF, JPEG, PNG, WEBP, DOCX, XLSX");
    if (f.size > MAX_DOC_BYTES) return toast.error("Document is too large. Please choose a file under 5MB.");
    setUploading(true);
    try {
      const file_data = await readFileAsDataUrl(f);
      await api.post("/documents", { customer_id: customer.id, doc_type: docType, title: docType, file_name: f.name, file_data });
      toast.success("Document uploaded");
      loadDocuments();
      window.dispatchEvent(new CustomEvent("ntaxco:data-changed", { detail: { resources: ["documents"] } }));
    } catch (err) {
      toast.error(describeApiError(err, "Unable to upload document"));
    } finally { setUploading(false); }
  };

  const downloadDocument = async (doc) => {
    try {
      const { data } = await api.get(`/documents/${encodeURIComponent(doc.id)}`);
      const full = data?.data;
      if (!full?.file_data) throw new Error("File data missing");
      const a = document.createElement("a");
      a.href = full.file_data;
      a.download = full.file_name || `${doc.doc_type || "document"}`;
      a.click();
    } catch (err) {
      toast.error(describeApiError(err, "Unable to download document"));
    }
  };

  const deleteDocument = async (doc) => {
    try { await removeDocument(doc.id); } catch { /* existing hook toast */ }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 flex-1">
          <div className="rounded-lg border border-zinc-200 bg-white p-3"><div className="text-[10px] uppercase font-bold text-zinc-500">Total Billed</div><div className="mt-1 text-lg font-bold text-slate-900">{inr(totals.billed)}</div></div>
          <div className="rounded-lg border border-emerald-100 bg-emerald-50/40 p-3"><div className="text-[10px] uppercase font-bold text-emerald-700">Total Paid</div><div className="mt-1 text-lg font-bold text-emerald-700">{inr(totals.paid)}</div></div>
          <div className="rounded-lg border border-red-100 bg-red-50/40 p-3"><div className="text-[10px] uppercase font-bold text-red-600">Outstanding</div><div className="mt-1 text-lg font-bold text-red-600">{inr(totals.outstanding)}</div></div>
        </div>
        {onEdit && <Button variant="outline" className="border-zinc-300" onClick={onEdit}><Pencil className="h-4 w-4 mr-1.5" />Edit</Button>}
      </div>

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList className="w-full justify-start flex-wrap h-auto bg-transparent border-b border-zinc-200 rounded-none gap-1">
          <TabsTrigger value="services" className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-700">◉&nbsp; Services</TabsTrigger>
          <TabsTrigger value="payments" className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-700">▣&nbsp; Payments</TabsTrigger>
          <TabsTrigger value="details" className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-700">◌&nbsp; Customer Details</TabsTrigger>
          <TabsTrigger value="type" className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-700">⊞&nbsp; Type</TabsTrigger>
          <TabsTrigger value="status" className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-700">✓&nbsp; Status</TabsTrigger>
        </TabsList>

        <TabsContent value="services" className="space-y-4 pt-4">
          <div className="flex flex-wrap gap-2">
            {["All Services", "GST", "Income Tax", "TDS", "ROC", "Accounting", "Others"].map((name) => (
              <button key={name} type="button" onClick={() => setServiceFilter(name)} className={`rounded-full border px-4 py-2 text-xs font-semibold transition ${serviceFilter === name ? "border-blue-600 bg-blue-600 text-white" : "border-zinc-200 bg-white text-zinc-600 hover:border-blue-200"}`}>{name}</button>
            ))}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {visibleServices.map((service) => (
              <div key={service.id} className="rounded-xl border border-zinc-200 bg-white p-4 hover:shadow-sm transition-shadow">
                <div className="flex items-start gap-3">
                  <div className="h-10 w-10 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center shrink-0"><ServiceIcon name={service.name} /></div>
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold text-slate-900 truncate">{service.name}</div>
                    <div className="text-xs text-zinc-500 mt-1">{service.source} · Consultant: {service.assigned_employee || "Unassigned"}</div>
                  </div>
                  <StatusBadge value={service.status || customer.status} />
                </div>
                <div className="grid grid-cols-2 gap-2 mt-4 text-[11px]">
                  <div><span className="text-zinc-400">Start</span><div className="font-medium text-zinc-700">{service.start_date || "—"}</div></div>
                  <div><span className="text-zinc-400">Due</span><div className="font-medium text-zinc-700">{service.due_date || "—"}</div></div>
                </div>
              </div>
            ))}
            {!visibleServices.length && <div className="col-span-full rounded-xl border border-dashed border-zinc-200 p-8 text-center text-sm text-zinc-500">No services linked to this customer yet.</div>}
          </div>

          <SectionCard title="Recent Bookings">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                ["New Bookings", bookings.filter((b) => String(b.status || "").toLowerCase() === "new").length, CalendarDays, "bg-blue-50 text-blue-600"],
                ["In Progress", bookings.filter((b) => ["In Progress","Running"].includes(b.status)).length, Loader2, "bg-amber-50 text-amber-600"],
                ["Confirmed", bookings.filter((b) => String(b.status || "") === "Confirmed").length, CheckCircle2, "bg-emerald-50 text-emerald-600"],
                ["Completed", bookings.filter((b) => String(b.status || "") === "Completed").length, CheckCircle2, "bg-violet-50 text-violet-600"],
              ].map(([label, value, Icon, cls]) => <div key={label} className={`rounded-lg border p-3 ${cls}`}><Icon className="h-4 w-4" /><div className="text-[11px] font-semibold mt-2">{label}</div><div className="text-xl font-bold">{value}</div></div>)}
            </div>
            <div className="mt-3 flex justify-end"><Button variant="outline" size="sm" className="border-zinc-300" onClick={() => toast.info("Open the existing Bookings module to manage all bookings.")}>View All</Button></div>
          </SectionCard>
        </TabsContent>

        <TabsContent value="payments" className="space-y-4 pt-4">
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {[
              ["Total Billed", totals.billed, ReceiptText, "text-slate-900", "bg-blue-50 text-blue-600"],
              ["Total Paid", totals.paid, CheckCircle2, "text-emerald-700", "bg-emerald-50 text-emerald-600"],
              ["Outstanding", totals.outstanding, WalletCards, "text-red-600", "bg-red-50 text-red-600"],
              ["Pending Payments", totals.pendingPayments, Clock3, "text-amber-700", "bg-amber-50 text-amber-600"],
            ].map(([label, value, Icon, valueCls, iconCls]) => <div key={label} className="rounded-xl border border-zinc-200 p-4 bg-white"><div className={`h-8 w-8 rounded-lg flex items-center justify-center ${iconCls}`}><Icon className="h-4 w-4" /></div><div className="text-[11px] text-zinc-500 mt-3">{label}</div><div className={`text-lg font-bold mt-1 ${valueCls}`}>{inr(value)}</div></div>)}
          </div>
          <div className="rounded-xl border border-zinc-200 overflow-x-auto">
            <table className="min-w-[900px] w-full text-xs">
              <thead className="bg-slate-50 border-b"><tr>{["Payment ID","Invoice ID","Service","Amount","Paid Amount","Balance","Payment Method","Payment Date","Due Date","Status"].map((h) => <th key={h} className="px-3 py-3 text-left font-bold text-zinc-500">{h}</th>)}</tr></thead>
              <tbody>{payments.map((p) => {
                const amount = Number(p.total || p.amount || 0), paid = Number(p.paid_amount || (String(p.status) === "Completed" ? amount : 0)), balance = Math.max(0, amount - paid);
                return <tr key={p.id} className="border-b border-zinc-100"><td className="px-3 py-3 font-medium">{p.payment_id || p.id}</td><td className="px-3 py-3">{p.invoice_no || p.invoice_id || "—"}</td><td className="px-3 py-3">{p.service || p.service_id || "—"}</td><td className="px-3 py-3">{inr(amount)}</td><td className="px-3 py-3 text-emerald-700">{inr(paid)}</td><td className="px-3 py-3 text-red-600">{inr(balance)}</td><td className="px-3 py-3">{p.payment_method || "—"}</td><td className="px-3 py-3">{p.payment_date || p.created_at || "—"}</td><td className="px-3 py-3">{p.due_date || "—"}</td><td className="px-3 py-3"><StatusBadge value={p.status} /></td></tr>;
              })}{!payments.length && <tr><td colSpan="10" className="p-8 text-center text-zinc-500">No payments recorded yet.</td></tr>}</tbody>
            </table>
          </div>
        </TabsContent>

        <TabsContent value="details" className="space-y-4 pt-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <SectionCard title="Basic Information">
              <Row label="Customer ID" value={customer.cust_id} /><Row label="Business Name" value={customer.business_name} />
              <Row label="Customer Type" value={customer.business_type} /><Row label="Registration Date" value={customer.registration_date} /><Row label="Account Status" value={<StatusBadge value={customer.status} />} />
            </SectionCard>
            <SectionCard title="Tax Information">
              <Row label="GSTIN" value={customer.gst_number} /><Row label="PAN" value={customer.pan} /><Row label="TAN" value={customer.tan} /><Row label="CIN" value={customer.cin} />
              <Row label="GST Registration Type" value={customer.gst_registration_type} /><Row label="Taxpayer Type" value={customer.taxpayer_type} />
            </SectionCard>
            <SectionCard title="Address">
              <Row label="State" value={customer.state} /><Row label="City" value={customer.city} /><Row label="Address" value={customer.address} /><Row label="Pincode" value={customer.pincode} />
              <Row label="Registered Address" value={customer.registered_address} /><Row label="Communication Address" value={customer.communication_address} />
            </SectionCard>
            <SectionCard title="Contact">
              <Row label="Primary Contact" value={customer.owner} /><Row label="Designation" value={customer.designation} /><Row label="Mobile" value={customer.mobile} />
              <Row label="Alternate Mobile" value={customer.alternate_mobile} /><Row label="Email" value={customer.email} /><Row label="Alternate Email" value={customer.alternate_email} /><Row label="Website" value={customer.website} />
            </SectionCard>
          </div>
          <SectionCard title="Customer Projects & Activity" className="scroll-mt-6" id="customer-projects-section">
            <RelationshipPanel entity="customer" recordId={customer.id} />
          </SectionCard>
          <div className="flex justify-end gap-2"><Button variant="outline" onClick={onEdit}><Pencil className="h-4 w-4 mr-1.5" />Edit Customer</Button></div>
        </TabsContent>

        <TabsContent value="type" className="space-y-4 pt-4">
          <SectionCard title="Customer Classification">
            <div className="flex flex-wrap gap-2">
              {["GST Customer","TDS Customer","Income Tax Customer","ROC Customer","Accounting Customer","Other"].map((label) => {
                const active = String(customer.service_type || "").toLowerCase() === label.replace(" Customer","").toLowerCase();
                return <span key={label} className={`rounded-full border px-4 py-2 text-xs font-semibold ${active ? "border-blue-600 bg-blue-600 text-white" : "border-zinc-200 bg-white text-zinc-600"}`}>{label}</span>;
              })}
            </div>
            <p className="text-xs text-zinc-500 mt-3">Classification shown here reflects the existing customer service fields. The backend currently exposes a primary service category rather than a separate multi-classification field.</p>
          </SectionCard>
        </TabsContent>

        <TabsContent value="status" className="space-y-4 pt-4">
          <SectionCard title="Current Status">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <div><div className="text-xs text-zinc-500 mb-1">Current Status</div><StatusBadge value={customer.status} /></div>
              <div><div className="text-xs text-zinc-500 mb-1">Payment / Filing Status</div><StatusBadge value={customer.filing_status} /></div>
              <div><div className="text-xs text-zinc-500 mb-1">Payment Frequency</div><span className="text-sm font-semibold">{customer.payment_frequency || "—"}</span></div>
            </div>
          </SectionCard>
          <SectionCard title="Status History">
            <div className="rounded-lg border border-dashed border-zinc-200 p-5 text-sm text-zinc-500">
              Current customer records expose the current status but do not expose a dedicated status-history collection in the existing customer response. No fabricated history is shown.
            </div>
          </SectionCard>
        </TabsContent>
      </Tabs>

      <SectionCard title="Customer Documents" className="scroll-mt-6" id="customer-documents-section">
        <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
          <div className="w-full sm:w-56"><Select value={docType} onValueChange={setDocType}><SelectTrigger className="border-zinc-300"><SelectValue /></SelectTrigger><SelectContent className="bg-white">{DOC_TYPES.map((d) => <SelectItem key={d} value={d}>{d}</SelectItem>)}</SelectContent></Select></div>
          <label className="inline-flex"><input type="file" accept={ALLOWED_DOC_MIMES.join(",")} className="hidden" onChange={onDocFile} disabled={uploading} /><span className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-medium cursor-pointer hover:bg-zinc-50">{uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}Upload</span></label>
        </div>
        {documents.length ? <div className="mt-3 overflow-x-auto"><table className="min-w-[700px] w-full text-xs"><thead className="bg-slate-50"><tr>{["Document Name","File Type","Uploaded By","Upload Date","File Size","Actions"].map((h) => <th key={h} className="px-3 py-2 text-left text-zinc-500">{h}</th>)}</tr></thead><tbody>{documents.map((d) => <tr key={d.id} className="border-t border-zinc-100"><td className="px-3 py-2 font-medium">{d.doc_type || d.title || d.file_name}</td><td className="px-3 py-2">{d.file_name?.split(".").pop()?.toUpperCase() || "—"}</td><td className="px-3 py-2">{d.uploaded_by || "—"}</td><td className="px-3 py-2">{d.uploaded_date || "—"}</td><td className="px-3 py-2">{d.file_size || "—"}</td><td className="px-3 py-2"><Button variant="ghost" size="sm" onClick={() => downloadDocument(d)}><Download className="h-4 w-4 mr-1" />Download</Button><Button variant="ghost" size="icon" onClick={() => deleteDocument(d)}><Trash2 className="h-4 w-4 text-red-500" /></Button></td></tr>)}</tbody></table></div> : <p className="mt-3 text-sm text-zinc-500">No documents uploaded yet.</p>}
      </SectionCard>
    </div>
  );
}
