import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "Doppelganger Operations",
  description:
    "One workspace for task completion, staleness, and module health.",
  icons: { icon: "./favicon.ico" },
  robots: { index: false, follow: false },
  openGraph: {
    title: "Doppelganger Operations",
    description:
      "Task completion, staleness, and module health in one workspace.",
  },
  twitter: {
    card: "summary",
    title: "Doppelganger Operations",
    description:
      "Task completion, staleness, and module health in one workspace.",
  },
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
