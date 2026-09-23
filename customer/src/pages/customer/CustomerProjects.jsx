import { useEffect, useState } from "react";
import PageHeader from "@/components/shared/PageHeader";
import SiteImageGallery from "@/components/shared/SiteImageGallery";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import api, { describeApiError } from "@/lib/api";
import { toast } from "sonner";
export default function CustomerProjects() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => { (async () => {
    try {
      const { data } = await api.get("/projects");
      const rows = data?.data?.projects || data?.projects || data?.data || [];
      setProjects(Array.isArray(rows) ? rows : []);
    } catch (e) { toast.error(describeApiError(e, "Could not load company projects")); }
    finally { setLoading(false); }
  })(); }, []);
  return <div className="max-w-6xl mx-auto px-6 py-8">
    <PageHeader title="Company Projects" subtitle="Explore projects completed by NTAXCO. These are view-only company showcases." breadcrumb={["Customer","Projects"]} />
    <SiteImageGallery placement="projects" title="Project Gallery" className="mb-8" container={false} />
    {loading ? <p className="text-muted-foreground">Loading projects...</p> : projects.length === 0 ? <Card><CardContent className="p-6 text-muted-foreground">No company projects are available yet.</CardContent></Card> :
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">{projects.map((p,i)=><Card key={p.id||p._id||i}><CardHeader><CardTitle>{p.title||p.name||"Company project"}</CardTitle></CardHeader><CardContent className="space-y-2"><p className="text-sm text-muted-foreground">{p.description||p.summary||"A project delivered by our company."}</p>{(p.status||p.category)&&<p className="text-xs font-medium">{p.category||p.status}</p>}</CardContent></Card>)}</div>}
  </div>;
}
