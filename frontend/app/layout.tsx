import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FrameNotes AI · Turn videos into visual notes",
  description: "Extract the slides and ideas that matter from YouTube videos.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
