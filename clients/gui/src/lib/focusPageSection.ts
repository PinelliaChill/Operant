/** Move within the mounted page without changing HashRouter's route. */
export function focusPageSection(id: string): void {
  const target = document.getElementById(id);
  if (!target) return;
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: 'nearest' });
}
