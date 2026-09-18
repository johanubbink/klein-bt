(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.getElementById("legend").open = true;
  await sleep(700);
  return "ready";
})()
