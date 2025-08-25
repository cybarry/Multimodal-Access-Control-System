// Simple timed refresh for the recent table on the dashboard
document.addEventListener("DOMContentLoaded", () => {
  const recentTable = document.getElementById("recent-logs");
  if (!recentTable) return;

  setInterval(() => {
    fetch(window.location.href, { cache: "no-store" })
      .then(r => r.text())
      .then(html => {
        const dom = new DOMParser().parseFromString(html, "text/html");
        const tbody = dom.querySelector("#recent-logs");
        if (tbody) recentTable.innerHTML = tbody.innerHTML;
      })
      .catch(() => {});
  }, 4000);
});
