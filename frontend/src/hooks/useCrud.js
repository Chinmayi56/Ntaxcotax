import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import api, { describeApiError } from "@/lib/api";
import { notifyDataChanged } from "@/lib/dataSync";

const RELATED_RESOURCES = {
  payments: ["payments", "invoices", "bookings", "customers", "agents"],
  invoices: ["invoices", "payments", "bookings", "customers"],
  bookings: ["bookings", "customers", "agents", "invoices"],
  customers: ["customers", "bookings", "invoices", "payments"],
  agents: ["agents", "bookings", "commissions", "payments"],
  services: ["services", "bookings", "invoices", "payments", "customers"],
};


export function useCrud(name) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get(`/${name}`);
      setRows(Array.isArray(data?.data) ? data.data : Array.isArray(data) ? data : []);
    } catch (e) {
      toast.error(describeApiError(e, `Failed to load ${name}`));
    } finally {
      setLoading(false);
    }
  }, [name]);

  useEffect(() => {
    load();
    const handler = (event) => {
      const resources = event?.detail?.resources || [];
      if (event?.detail?.source === name) return;
      if (resources.includes(name) || resources.includes("*")) load();
    };
    window.addEventListener("ntaxco:data-changed", handler);
    return () => window.removeEventListener("ntaxco:data-changed", handler);
  }, [load, name]);

  const create = async (body) => {
    const { data } = await api.post(`/${name}`, body);
    await load();
    toast.success("Record created");
    notifyDataChanged(RELATED_RESOURCES[name] || [name], name);
    return data?.data;
  };

  const update = async (id, body) => {
    const { data } = await api.put(`/${name}/${id}`, body);
    await load();
    toast.success("Record updated");
    notifyDataChanged(RELATED_RESOURCES[name] || [name], name);
    return data?.data;
  };

  const remove = async (id) => {
    try {
      const { data } = await api.delete(`/${name}/${id}`);
      await load();
      toast.success("Record deleted");
      notifyDataChanged([name], name);
      return data?.data;
    } catch (e) {
      // e.g. a service/customer that still has linked bookings, invoices or
      // payments: the backend rejects the hard delete (409) to preserve
      // historical records, and the caller should see why.
      toast.error(describeApiError(e, `Failed to delete ${name.slice(0, -1)}`));
      throw e;
    }
  };

  return { rows, loading, load, create, update, remove };
}
