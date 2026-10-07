import { redirect } from "next/navigation";

// 006-web T020: the home page is the runs list (SCR-02).
export default function Home() {
  redirect("/runs");
}
