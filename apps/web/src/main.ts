/** Fail explicitly if the document contract drifts; textContent keeps status copy out of HTML parsing. */
function initializeWorkspace(): void {
  const status = document.querySelector<HTMLParagraphElement>("#runtime-status");
  if (!status) throw new Error("CreditLens runtime status element is missing.");
  status.textContent = "TypeScript application ready. API integration is pending.";
}

initializeWorkspace();
