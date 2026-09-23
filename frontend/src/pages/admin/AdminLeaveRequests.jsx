import { useCrud } from "@/hooks/useCrud";
import PageHeader from "@/components/shared/PageHeader";
import DataTable from "@/components/shared/DataTable";
import { KpiCard } from "@/components/shared/KpiCard";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { Button } from "@/components/ui/button";
import { PlaneTakeoff, CheckCircle2, Clock, XCircle, Check, X } from "lucide-react";
import { toast } from "sonner";

export default function AdminLeaveRequests() {
  const { rows, loading, update } = useCrud("leaves");

  const act = async (row, status) => {
    try {
      await update(row.id, { status });
      toast.success(`Leave request ${status.toLowerCase()}`);
    } catch (e) {
      toast.error("Could not update the leave request");
    }
  };

  const kpis = [
    { title: "Total Requests", value: rows.length, icon: PlaneTakeoff },
    { title: "Approved", value: rows.filter((r) => r.status === "Approved").length, icon: CheckCircle2 },
    { title: "Pending", value: rows.filter((r) => r.status === "Pending").length, icon: Clock },
    { title: "Rejected", value: rows.filter((r) => r.status === "Rejected").length, icon: XCircle },
  ];

  const columns = [
    { key: "employee_name", label: "Employee" },
    { key: "employee_id", label: "Employee ID" },
    { key: "leave_type", label: "Type" },
    { key: "from_date", label: "From" },
    { key: "to_date", label: "To" },
    { key: "days", label: "Days" },
    { key: "reason", label: "Reason" },
    { key: "status", label: "Status", render: (r) => <StatusBadge value={r.status} /> },
    {
      key: "actions", label: "Actions", render: (r) => (
        r.status === "Pending" ? (
          <div className="flex items-center gap-2">
            <Button size="sm" className="bg-emerald-600 hover:bg-emerald-700 text-white h-7 px-2" onClick={() => act(r, "Approved")} data-testid={`approve-${r.id}`}><Check className="h-3.5 w-3.5 mr-1" />Approve</Button>
            <Button size="sm" variant="outline" className="border-red-300 text-red-600 hover:bg-red-50 h-7 px-2" onClick={() => act(r, "Rejected")} data-testid={`reject-${r.id}`}><X className="h-3.5 w-3.5 mr-1" />Reject</Button>
          </div>
        ) : <span className="text-xs text-muted-foreground">No action needed</span>
      )
    },
  ];

  return (
    <div>
      <PageHeader title="Leave Requests" breadcrumb={["Admin", "Leave Requests"]} subtitle="Review and approve or reject employee leave requests." />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {kpis.map((k, i) => <KpiCard key={i} {...k} loading={loading} testId={`leave-kpi-${i}`} />)}
      </div>
      <DataTable title="Leave Requests" columns={columns} rows={rows} loading={loading} pageSize={10} testId="leave-requests-table" />
    </div>
  );
}
