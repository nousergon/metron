import { redirect } from "next/navigation";

// Glance was renamed Insights (Brian, 2026-11-04). Old links and bookmarks keep working.
export default async function GlanceRedirect(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  redirect(`/portfolios/${id}/insights`);
}
