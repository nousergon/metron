"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { createPortfolio } from "@/lib/api";
import { requireApiAuth } from "@/lib/session";

export type CreateResult = { ok: false; message: string };

/** Create a portfolio in the signed-in user's workspace, then open it. */
export async function createPortfolioAction(formData: FormData): Promise<CreateResult | void> {
  const name = String(formData.get("name") ?? "").trim();
  if (!name) return { ok: false, message: "Give your portfolio a name." };

  const apiAuth = await requireApiAuth();
  let id: string;
  try {
    id = (await createPortfolio(apiAuth, name)).id;
  } catch {
    return { ok: false, message: "Couldn't create the portfolio — is the backend reachable?" };
  }
  revalidatePath("/");
  redirect(`/portfolios/${id}`); // throws NEXT_REDIRECT — must be outside the try
}
