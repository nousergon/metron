import { MagicLinkForm } from "@/components/auth-forms";

export const metadata = { title: "Sign in — Metron" };

export default function LoginPage() {
  return (
    <div>
      <MagicLinkForm />
      <p className="mt-6 text-center text-sm text-muted">
        Just looking?{" "}
        <a href="/demo" className="font-medium text-accent hover:underline">
          Explore the live demo
        </a>{" "}
        — read-only, no signup.
      </p>
    </div>
  );
}
