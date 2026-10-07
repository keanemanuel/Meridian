import type { Metadata } from "next";
import { Jost, Rubik_Distressed, Special_Elite } from "next/font/google";
import "./globals.css";
import {
  RevealBoundary,
  RevealControl,
  RevealProvider,
} from "@/components/RevealProvider";
import { Sidebar } from "@/components/Sidebar";
import { ToastProvider } from "@/components/Toast";
import { WorkspacesProvider } from "@/components/WorkspacesProvider";

// Body copy and data. Geometric sans, close to the reference's Futura.
const jost = Jost({ variable: "--font-jost", subsets: ["latin"] });
// Labels, nav, buttons, metadata.
const specialElite = Special_Elite({
  variable: "--font-special-elite",
  weight: "400",
  subsets: ["latin"],
});
// Page titles only.
const rubikDistressed = Rubik_Distressed({
  variable: "--font-rubik-distressed",
  weight: "400",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "IFF Recruitment",
  description: "Interview scheduler for IFF recruitment.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${jost.variable} ${specialElite.variable} ${rubikDistressed.variable} h-full antialiased`}
    >
      <body className="h-full">
        <ToastProvider>
          <RevealProvider>
            <WorkspacesProvider>
              <div className="flex h-full flex-col">
                <header className="filmstrip flex h-16 shrink-0 items-center justify-between gap-4 px-5">
                  <span className="type-label pt-0.5 text-sm text-paper">
                    IFF Recruitment
                  </span>
                  <div className="flex items-center gap-4">
                    <RevealControl />
                    <span
                      className="rec hidden pt-0.5 text-purple-tint sm:inline-flex"
                      aria-hidden="true"
                    >
                      REC 00:00:00
                    </span>
                  </div>
                </header>
                <div className="flex min-h-0 flex-1 flex-col md:flex-row">
                  <Sidebar />
                  <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
                    <RevealBoundary>{children}</RevealBoundary>
                  </main>
                </div>
              </div>
            </WorkspacesProvider>
          </RevealProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
