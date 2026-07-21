const services = [
  { id: "wallet-status", url: "http://localhost:3100/health" },
  { id: "explorer-status", url: "http://localhost:3001/health" },
];

const savedTheme = localStorage.getItem("perkbridge.theme") || "dark";
document.documentElement.dataset.theme = savedTheme;

function renderTheme() {
  const theme = document.documentElement.dataset.theme;
  document.querySelector(".theme-toggle").textContent = theme === "dark" ? "Light mode" : "Dark mode";
  document.getElementById("wallet-link").href = `http://localhost:3100/?theme=${theme}`;
}

document.querySelector(".theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("perkbridge.theme", theme);
  renderTheme();
});

renderTheme();

async function updateStatus(service) {
  const element = document.getElementById(service.id);
  try {
    await fetch(service.url, { mode: "no-cors", cache: "no-store" });
    element.className = "status online";
    element.innerHTML = "<i></i>Online";
  } catch {
    element.className = "status offline";
    element.innerHTML = "<i></i>Offline";
  }
}

services.forEach(updateStatus);
setInterval(() => services.forEach(updateStatus), 10000);
