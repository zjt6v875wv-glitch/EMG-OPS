import "./globals.css";
import { SessionProvider } from "@/lib/session";

export const metadata = { title: "EMG Operations", description: "Commission payroll, statements, reporting and sales mapping" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
