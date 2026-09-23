import { useState } from "react";
import { useCrud } from "@/hooks/useCrud";
import PageHeader from "@/components/shared/PageHeader";
import DataTable from "@/components/shared/DataTable";
import { KpiCard } from "@/components/shared/KpiCard";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { Button } from "@/components/ui/button";
import { Wallet, CheckCircle2, Clock, AlertCircle, Download } from "lucide-react";
import { exportPDF } from "@/lib/exports";
import { createOrder, verifyPayment } from "@/lib/payments";
import { inr } from "@/lib/utils";
import { toast } from "sonner";

export default function CustomerPayments() {
  const { rows, loading, load } = useCrud("invoices");
  const [processing,setProcessing]=useState(false);
  const unpaid=rows.filter(r=>r.payment_status!=="Paid"), paid=rows.filter(r=>r.payment_status==="Paid");
  const outstanding=unpaid.reduce((s,r)=>s+Number(r.balance ?? r.total ?? 0),0);
  const receipt=(inv)=>{exportPDF(`Receipt ${inv.invoice_no}`,[{key:"k",label:"Field"},{key:"v",label:"Value"}],[{k:"Receipt For",v:inv.invoice_no},{k:"Customer",v:inv.customer},{k:"Amount Paid",v:inr(inv.total)},{k:"Transaction ID",v:inv.txn||inv.payment_id||"—"},{k:"Method",v:"Razorpay"},{k:"Status",v:"Verified"}]);};
  const pay=async(inv)=>{
    if(!window.Razorpay){toast.error("Razorpay Checkout is unavailable. Check the customer portal network connection.");return;}
    setProcessing(true);
    try{
      const order=(await createOrder({amount:Number(inv.total),invoice_id:inv.id})); 
      const options={key:order.key_id,amount:order.amount,currency:order.currency,name:"NTAXCO",description:`Invoice ${inv.invoice_no}`,order_id:order.order_id,
        handler:async(response)=>{try{await verifyPayment({order_id:response.razorpay_order_id,payment_id:response.razorpay_payment_id,signature:response.razorpay_signature,amount:inv.total,invoice_id:inv.id});toast.success("Payment verified successfully");await load();}catch(e){toast.error(e?.response?.data?.detail||"Payment verification failed");}finally{setProcessing(false);}},
        modal:{ondismiss:()=>setProcessing(false)},theme:{color:"#FFB800"}};
      const checkout=new window.Razorpay(options); checkout.open();
    }catch(e){setProcessing(false);toast.error(e?.response?.data?.detail||"Unable to start payment");}
  };
  const columns=[{key:"invoice_no",label:"Invoice"},{key:"invoice_date",label:"Date"},{key:"total",label:"Amount",render:r=>inr(r.total),exportValue:r=>r.total},{key:"payment_status",label:"Status",render:r=><StatusBadge value={r.payment_status}/>},{key:"actions",label:"Actions",render:r=>r.payment_status==="Paid"?<Button variant="ghost" size="sm" className="text-brand-hover" onClick={()=>receipt(r)}><Download className="h-4 w-4 mr-1"/>Receipt</Button>:<Button size="sm" disabled={processing} className="bg-brand text-zinc-900 hover:bg-brand-hover font-semibold" onClick={()=>pay(r)}>Pay Now</Button>}];
  return <div className="max-w-6xl mx-auto px-6 py-8">
    <PageHeader title="Payments" breadcrumb={["Customer","Payments"]} subtitle="Pay outstanding invoices through verified Razorpay checkout."/>
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6"><KpiCard title="Outstanding" value={inr(outstanding)} icon={AlertCircle} loading={loading}/><KpiCard title="Paid Invoices" value={paid.length} icon={CheckCircle2} loading={loading}/><KpiCard title="Pending" value={unpaid.length} icon={Clock} loading={loading}/><KpiCard title="Total Billed" value={inr(rows.reduce((s,r)=>s+Number(r.total||0),0))} icon={Wallet} loading={loading}/></div>
    <DataTable title="Payments" columns={columns} rows={rows} loading={loading} pageSize={8} testId="payments-table"/>
  </div>;
}
