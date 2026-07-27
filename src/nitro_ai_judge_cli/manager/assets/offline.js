(() => {
  "use strict";
  const button = document.querySelector("#copy-start-command");
  const status = document.querySelector("#copy-status");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText("naij play manager start");
      status.textContent = "Command copied.";
    } catch (_error) {
      status.textContent = "Copy failed. Select the command above.";
    }
  });
})();
