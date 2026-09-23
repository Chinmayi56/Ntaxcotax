import { useState, useEffect } from "react";
import { Users, Building2, UserCog, FileText, FolderKanban, CheckCircle2, Clock, Wallet, Receipt, CalendarCheck, AlertCircle, RefreshCw, IndianRupee } from "lucide-react";
import PageHeader from "@/components/shared/PageHeader";
import { KpiCard } from "@/components/shared/KpiCard";
import { ChartCard, AreaChartView, BarChartView, LineChartView, DonutChartView } from "@/components/shared/Charts";
import { ActivityFeed, DueDatesWidget } from "@/components/shared/Widgets";
import RemindersWidget from "@/components/shared/RemindersWidget";
import { Button } from "@/components/ui/button";
import api, { describeApiError } from "@/lib/api";
import { toast } from "sonner";
import { inr } from "@/lib/utils";

const B = "/admin";
export default function AdminDashboard() {
  const [loading, setLoading] = useState(true); const [summary, setSummary] = useState(null); const [analytics, setAnalytics] = useState(null);
  const load = async () => { setLoading(true); try { const [{ data: d }, { data: a }] = await Promise.all([api.get("/admin/dashboard"), api.get("/admin/analytics/summary")]); setSummary(d?.data || null); setAnalytics(a?.data || null); } catch (e) { toast.error(describeApiError(e, "Unable to load dashboard")); } finally { setLoading(false); } };
  useEffect(() => {
    load();
    const onChange = (event) => {
      const resources = event?.detail?.resources || [];
      if (resources.some((r) => ["customers", "bookings", "invoices", "payments", "agents", "services"].includes(r))) load();
    };
    window.addEventListener("ntaxco:data-changed", onChange);
    return () => window.removeEventListener("ntaxco:data-changed", onChange);
  }, []);
  const c = summary?.cards || {}; const projects = summary?.projects || []; const charts = summary?.charts || {}; const serviceCounts = analytics?.service_counts || {}; const paymentCounts = analytics?.payment_status_counts || {}; const bookingCounts = analytics?.booking_status_counts || {};
  const cards = [{ title: "Total Customers", value: c.customers ?? 0, icon: Building2, to: `${B}/customers` }, { title: "Active Agents", value: c.active_agents ?? 0, icon: UserCog, to: `${B}/agents` }, { title: "Revenue Collected", value: inr(analytics?.invoice_financials?.paid ?? c.revenue ?? 0), icon: Wallet, to: `${B}/reports` }, { title: "Outstanding", value: inr(analytics?.invoice_financials?.balance ?? c.outstanding ?? 0), icon: AlertCircle, to: `${B}/payments` }, { title: "Invoices", value: analytics?.invoice_financials?.invoiced != null ? inr(analytics.invoice_financials.invoiced) : 0, icon: Receipt, to: `${B}/invoices` }, { title: "Collection Rate", value: `${analytics?.collection_rate ?? 0}%`, icon: IndianRupee, to: `${B}/payments` }, { title: "Bookings", value: c.bookings ?? 0, icon: CalendarCheck, to: `${B}/bookings` }, { title: "Employees", value: c.employees ?? 0, icon: Users, to: `${B}/employees` }];
  const statusData = Object.entries(bookingCounts).map(([name, value]) => ({ name, value })); const serviceData = Object.entries(serviceCounts).map(([name, value]) => ({ name, value })); const paymentData = Object.entries(paymentCounts).map(([name, value]) => ({ name, value }));
  const recentProjects = [...projects].sort((a,b) => String(b.updated_at || b.start_date || "").localeCompare(String(a.updated_at || a.start_date || ""))).slice(0, 5);
  return <div><PageHeader title="Super Admin Dashboard" subtitle="Overview of NTAXCO operations, revenue and compliance." breadcrumb={["Super Admin", "Dashboard"]} actions={<Button variant="outline" className="border-zinc-300" onClick={load}><RefreshCw className="h-4 w-4 mr-1.5" />Refresh</Button>} />
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 mb-6">{cards.map((x, i) => <KpiCard key={i} {...x} loading={loading} testId={`kpi-${i}`} />)}</div>
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6"><ChartCard title="Monthly Revenue" testId="chart-revenue"><AreaChartView data={charts.revenue_monthly || []} xKey="m" keys={[{ key: "revenue", name: "Revenue" }]} /></ChartCard><ChartCard title="Service-wise Invoice Counts"><DonutChartView data={serviceData} /></ChartCard><ChartCard title="Payment Status"><DonutChartView data={paymentData} /></ChartCard></div>
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6"><ChartCard title="Booking Status"><DonutChartView data={statusData} /></ChartCard><ChartCard title="Customer Growth"><BarChartView data={charts.customer_growth || []} xKey="m" keys={[{ key: "customers", name: "New Customers" }]} /></ChartCard></div>
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6"><ChartCard title="GST vs Income Tax Filing Trend"><LineChartView data={charts.filing_trend || []} xKey="m" keys={[{ key: "gst", name: "GST" }, { key: "itr", name: "ITR" }]} /></ChartCard><ChartCard title="Employee Performance"><BarChartView data={charts.performance || []} xKey="name" keys={[{ key: "score", name: "Score" }]} /></ChartCard></div>
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6"><ActivityFeed items={recentProjects.map(p => ({ title: `${p.name} — ${p.status}`, time: p.due_date ? `Due ${p.due_date}` : "Active", tag: p.service_type || "Project" }))} /><DueDatesWidget items={projects.filter(p => p.due_date).slice(0, 6).map(p => ({ title: p.name, date: p.due_date, type: p.service_type || "Project" }))} /></div><RemindersWidget />
  </div>;
}
