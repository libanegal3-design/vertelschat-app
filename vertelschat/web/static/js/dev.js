/* Development only: keep the simulated chat scrolled to the newest message (or the start with ?top=1). */
(function () { var c = document.getElementById("chat"); if (c && !/[?&]top=1/.test(location.search)) c.scrollTop = c.scrollHeight; })();
