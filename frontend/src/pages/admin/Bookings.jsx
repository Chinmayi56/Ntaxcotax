import CrudModule from "@/components/shared/CrudModule";
import { StatusBadge, PriorityBadge } from "@/components/shared/StatusBadge";
import ServiceBreakdown from "@/components/shared/ServiceBreakdown";
import { inr } from "@/lib/utils";
import { CalendarCheck, CalendarClock, Clock, CheckCircle2 } from "lucide-react";
import DragDropWorkflow from "@/components/shared/DragDropWorkflow";

const STATUS = ["Pending", "Running", "Confirmed", "Completed"];
const PRIORITY = ["Low", "Medium", "High"];
const PAY = ["Paid", "Pending", "Partial"];

const todayStr = () => new Date().toISOString().slice(0, 10);

export default function Bookings() {
  return (
    <CrudModule
      title="Bookings"
      singular="Booking"
      name="bookings"
      breadcrumb={["Super Admin", "Bookings"]}
      subtitle="All service bookings — status, priority and payment, live from the NTAXCO backend."
      kpiFn={(rows) => {
        const today = todayStr();
        return [
          { title: "Total Bookings", value: rows.length, icon: CalendarCheck, tone: "royal" },
          { title: "Today's Bookings", value: rows.filter((r) => r.booking_date === today).length, icon: CalendarClock, tone: "royal" },
          { title: "New Bookings", value: rows.filter((r) => r.status === "Pending").length, icon: Clock, tone: "red" },
          { title: "Completed", value: rows.filter((r) => r.status === "Completed").length, icon: CheckCircle2, tone: "green" },
        ];
      }}
      extra={(rows, loading, load) => (
        <div className="space-y-4">
          <ServiceBreakdown rows={rows} field="service" title="Bookings by Service" testId="bookings-service-breakdown" />
          <DragDropWorkflow
            title="Booking → Service"
            description="Optional service reassignment for bookings. Only canonical backend services are accepted."
            sourceRows={rows}
            sourceType="booking"
            targetType="service"
            sourceLabel="booking"
            getSourceLabel={(r) => `${r.booking_no || r.id} · ${r.customer || "Customer"}`}
            getTargetLabel={(r) => r.name || r.title || r.category || r.id}
            getTargetValue={(r) => r.id}
            onRefresh={load}
          />
        </div>
      )}

      columns={[
        { key: "booking_no", label: "Booking No" },
        { key: "customer", label: "Customer" },
        { key: "service", label: "Service" },
        { key: "assigned_employee", label: "Employee" },
        { key: "assigned_agent", label: "Agent" },
        { key: "booking_date", label: "Booked" },
        { key: "due_date", label: "Due" },
        { key: "estimated_fee", label: "Amount", render: (r) => (r.estimated_fee ? inr(r.estimated_fee) : "—"), exportValue: (r) => r.estimated_fee },
        { key: "priority", label: "Priority", render: (r) => <PriorityBadge value={r.priority} /> },
        { key: "status", label: "Status", render: (r) => <StatusBadge value={r.status} /> },
        { key: "payment_status", label: "Payment", render: (r) => <StatusBadge value={r.payment_status} /> },
      ]}
      fields={[
        { key: "booking_no", label: "Booking No", required: true },
        { key: "customer_id", label: "Customer", type: "relation", resource: "customers", required: true },
        { key: "service_id", label: "Service", type: "relation", resource: "services", required: true },
        { key: "assigned_employee", label: "Assigned Employee" },
        { key: "agent_id", label: "Assigned Agent", type: "relation", resource: "agents" },
        { key: "booking_date", label: "Booking Date", type: "date" },
        { key: "due_date", label: "Due Date", type: "date" },
        { key: "estimated_fee", label: "Amount (₹)", type: "number" },
        { key: "priority", label: "Priority", type: "select", options: PRIORITY, default: "Medium" },
        { key: "status", label: "Status", type: "select", options: STATUS, default: "Pending" },
        { key: "payment_status", label: "Payment Status", type: "select", options: PAY, default: "Pending" },
      ]}
    relationshipEntity="booking"
    />
  );
}
