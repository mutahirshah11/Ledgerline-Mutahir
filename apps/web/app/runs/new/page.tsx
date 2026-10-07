import { NewRun } from "./NewRun";

// 006-web T035/T036: SCR-03 `/runs/new` (`?run={id}` resumes), a client screen
// (contracts/screens.md SCR-03). Next 16: `searchParams` is a Promise.
export default async function NewRunPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { run } = await searchParams;
  return <NewRun resumeId={typeof run === "string" && run ? run : null} />;
}
