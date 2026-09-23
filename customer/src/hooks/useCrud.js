import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";

export function useCrud(name) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get(`/${name}`);
      setRows(data.data || []);
    } catch (e) {
      toast.error(formatApiError(e.response?.data?.detail) || `Failed to load ${name}`);
    } finally {
      setLoading(false);
    }
  }, [name]);

  useEffect(() => { load(); }, [load]);

  const create = async (body) => {
    await api.post(`/${name}`, body);
    toast.success("Record created");
    await load();
  };
  const update = async (id, body) => {
    await api.put(`/${name}/${id}`, body);
    toast.success("Record updated");
    await load();
  };
  const remove = async (id) => {
    await api.delete(`/${name}/${id}`);
    toast.success("Record deleted");
    await load();
  };

  return { rows, loading, load, create, update, remove };
}
