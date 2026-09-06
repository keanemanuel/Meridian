import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Sidebar } from "@/components/Sidebar";
import { ToastProvider } from "@/components/Toast";
import { WorkspacesProvider } from "@/components/WorkspacesProvider";

const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Meridian",
  description: "Interview scheduler for IFF recruitment.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} h-full antialiased`}>
      <body className="h-full">
        <ToastProvider>
          <WorkspacesProvider>
            <div className="flex h-full flex-col">
              <header className="flex h-12 shrink-0 items-center border-b border-neutral-200 bg-white px-5">
                <span className="text-sm font-semibold tracking-tight text-neutral-900">
                  Meridian
                </span>
              </header>
              <div className="flex min-h-0 flex-1">
                <Sidebar />
                <main className="min-w-0 flex-1 overflow-y-auto">
                  {children}
                </main>
              </div>
            </div>
          </WorkspacesProvider>
        </ToastProvider>
      </body>
    </html>
  );
}
