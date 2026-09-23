import { useEffect, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import PageHeader from "@/components/shared/PageHeader";
import api, { describeApiError } from "@/lib/api";
import { Building2, Mail, Phone, MapPin, FileText, Hash } from "lucide-react";
import { toast } from "sonner";

export default function CustomerProfile() {
  const { user } = useAuth();
  const [profile, setProfile] = useState(null);
  useEffect(() => {
    if (!user) return;
    api.get("/customers").then(({ data }) => setProfile(data?.data?.[0] || null))
      .catch((e) => toast.error(describeApiError(e, "Unable to load company profile")));
  }, [user]);
  const p = profile || {};
  const fields = [
    { icon: Building2, label: "Business Name", value: p.business_name || user?.name },
    { icon: FileText, label: "Owner", value: p.owner },
    { icon: Hash, label: "GST Number", value: p.gst_number },
    { icon: Hash, label: "PAN", value: p.pan },
    { icon: Hash, label: "TAN", value: p.tan },
    { icon: Hash, label: "CIN", value: p.cin },
    { icon: FileText, label: "Business Type", value: p.business_type },
    { icon: MapPin, label: "State", value: p.state },
    { icon: MapPin, label: "City", value: p.city },
    { icon: Mail, label: "Email", value: p.email || user?.email },
    { icon: Phone, label: "Mobile", value: p.mobile || user?.mobile },
    { icon: FileText, label: "Authorized Signatory", value: p.owner },
  ];
  return (
    <div className="max-w-4xl mx-auto px-6 py-8">
      <PageHeader title="Business Profile" breadcrumb={["Customer", "Profile"]} subtitle="Your registered business details." />
      <div className="bg-white border border-zinc-200 rounded-2xl shadow-sm overflow-hidden">
        <div className="bg-gradient-to-br from-brand-light to-brand-faint p-6 flex items-center gap-4">
          <div className="h-16 w-16 rounded-2xl bg-white shadow-sm flex items-center justify-center text-brand-hover"><Building2 className="h-8 w-8" /></div>
          <div><h2 className="font-heading text-xl font-bold text-zinc-900">{p.business_name || user?.name}</h2><p className="text-sm text-zinc-600">Customer ID: {p.cust_id || user?.meta?.customer_id || "—"}</p></div>
        </div>
        <div className="p-6 grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-1">
          {fields.map((f) => <div key={f.label} className="flex items-center gap-3 py-3 border-b border-zinc-100"><f.icon className="h-4 w-4 text-zinc-400 shrink-0" /><span className="text-sm text-muted-foreground w-40">{f.label}</span><span className="text-sm font-medium text-zinc-800 ml-auto text-right">{f.value || "—"}</span></div>)}
        </div>
      </div>
    </div>
  );
}
