(() => {
  const elDate = document.getElementById('date-text');
  const elDay = document.getElementById('day-text');

  const h1 = document.getElementById('h1');
  const h2 = document.getElementById('h2');
  const colon = document.getElementById('colon');
  const m1 = document.getElementById('m1');
  const m2 = document.getElementById('m2');

  const DAYS_JA = ['日', '月', '火', '水', '木', '金', '土'];

  function updateWidget() {
    const now = new Date();

    // 1. 日付と曜日（「/」区切り）
    const month = now.getMonth() + 1;
    const date = now.getDate();
    const dayIdx = now.getDay();

    // 例: 「9/26」
    // ※「09/26」のように常に2桁（ゼロ埋め）に揃えたい場合は
    //   `${String(month).padStart(2, '0')}/${String(date).padStart(2, '0')}` に変更してください
    elDate.textContent = `${month}/${date}`;
    elDay.textContent = `(${DAYS_JA[dayIdx]})`;

    elDay.className = 'day-text';
    if (dayIdx === 6) elDay.classList.add('sat');
    if (dayIdx === 0) elDay.classList.add('sun');

    // 2. 時刻 (hh:mm)
    const hh = String(now.getHours()).padStart(2, '0');
    const mm = String(now.getMinutes()).padStart(2, '0');

    h1.textContent = hh.charAt(0);
    h2.textContent = hh.charAt(1);
    m1.textContent = mm.charAt(0);
    m2.textContent = mm.charAt(1);

    // 3. コロンの秒同期ブリンク
    const isEvenSecond = (now.getSeconds() % 2 === 0);
    if (isEvenSecond) {
      colon.classList.remove('blink-off');
    } else {
      colon.classList.add('blink-off');
    }
  }

  updateWidget();

  function loop() {
    updateWidget();
    const now = new Date();
    const delay = 1000 - now.getMilliseconds();
    setTimeout(loop, delay);
  }
  loop();
})();
