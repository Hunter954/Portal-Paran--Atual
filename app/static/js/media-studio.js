(() => {
  const root = document.querySelector('[data-pa-studio]');
  if (!root) return;
  const form = root.querySelector('form');
  const format = form.elements.format;
  const source = form.elements.source;
  const status = root.querySelector('[data-studio-status]');
  const canvas = root.querySelector('canvas');
  const video = root.querySelector('video');
  const download = root.querySelector('[data-studio-download]');
  const context = canvas.getContext('2d', { alpha: false });
  let outputURL;
  let audioContext;
  let audioSource;
  let audioOutput;

  const logo = new Image();
  logo.src = root.dataset.logo;

  function dimensions(file) {
    return file === video ? [video.videoWidth, video.videoHeight] : [file.naturalWidth, file.naturalHeight];
  }

  function titleLines(headline, size) {
    context.font = '900 ' + size + 'px system-ui, sans-serif';
    const lines = [''];
    for (const word of headline.trim().split(/\s+/)) {
      const last = lines.length - 1;
      const trial = lines[last] ? lines[last] + ' ' + word : word;
      if (lines[last] && context.measureText(trial).width > 940) lines.push(word);
      else lines[last] = trial;
    }
    return lines;
  }

  function render(file, headline) {
    const [mediaWidth, mediaHeight] = dimensions(file);
    const mediaEnd = canvas.height === 1920 ? 1295 : 920;
    const mediaHeightTarget = mediaEnd - 125;
    const scale = Math.max(canvas.width / mediaWidth, mediaHeightTarget / mediaHeight);
    const width = mediaWidth * scale;
    const height = mediaHeight * scale;
    context.fillStyle = '#180c46';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.save();
    context.beginPath();
    context.rect(0, 125, canvas.width, mediaHeightTarget);
    context.clip();
    context.drawImage(file, (1080 - width) / 2, 125 + (mediaHeightTarget - height) / 2, width, height);
    context.restore();

    if (logo.complete && logo.naturalWidth) {
      const widthLogo = Math.min(184, 92 * logo.naturalWidth / logo.naturalHeight);
      context.drawImage(logo, 64, 62 - widthLogo * logo.naturalHeight / logo.naturalWidth / 2,
        widthLogo, widthLogo * logo.naturalHeight / logo.naturalWidth);
    } else {
      context.font = '900 38px system-ui, sans-serif';
      context.fillStyle = '#fff';
      context.fillText('PARANÁ ATUAL', 64, 81);
    }
    context.fillStyle = '#8e76ff';
    context.fillRect(64, mediaEnd + 48, 110, 8);
    let size = canvas.height === 1920 ? 76 : 59;
    let lines = titleLines(headline, size);
    const available = canvas.height - mediaEnd - 161;
    while (lines.length * size * 1.14 > available && size > 34) {
      size -= 2;
      lines = titleLines(headline, size);
    }
    context.fillStyle = '#fff';
    lines.forEach((line, index) => context.fillText(line, 64, mediaEnd + 73 + size + index * size * 1.14, 950));
    context.font = '650 31px system-ui, sans-serif';
    context.fillStyle = '#d2c8ff';
    context.fillText('PARANAATUAL.COM.BR', 64, canvas.height - 40);
  }

  function setDownload(blob, extension) {
    if (!blob || !blob.size) throw new Error('O navegador não gerou o arquivo.');
    if (outputURL) URL.revokeObjectURL(outputURL);
    outputURL = URL.createObjectURL(blob);
    download.href = outputURL;
    download.download = 'Parana-Atual-' + format.value + '.' + extension;
    download.hidden = false;
    status.textContent = 'Arquivo pronto para baixar.';
  }

  async function renderVideo(uploadURL, title) {
    if (!canvas.captureStream || typeof MediaRecorder === 'undefined') {
      throw new Error('Este navegador não oferece exportação de vídeo. Abra no Chrome ou no Edge.');
    }
    video.src = uploadURL;
    await new Promise((resolve, reject) => {
      video.onloadeddata = resolve;
      video.onerror = () => reject(new Error('Não foi possível abrir esse vídeo.'));
    });
    if (!Number.isFinite(video.duration) || video.duration <= 0 || video.duration > 60) {
      throw new Error('Escolha um vídeo de até 60 segundos.');
    }
    const stream = canvas.captureStream(25);
    let animation = 0;
    let recorder;
    try {
      if (audioContext) audioOutput.stream.getAudioTracks().forEach(track => stream.addTrack(track.clone()));
      const mime = ['video/mp4;codecs=avc1.420028,mp4a.40.2', 'video/webm;codecs=vp9,opus',
        'video/webm;codecs=vp8,opus', 'video/webm'].find(type => MediaRecorder.isTypeSupported(type));
      recorder = new MediaRecorder(stream, { ...(mime ? { mimeType: mime } : {}), videoBitsPerSecond: 4500000 });
      const parts = [];
      const finished = new Promise((resolve, reject) => {
        recorder.ondataavailable = event => { if (event.data.size) parts.push(event.data); };
        recorder.onstop = () => resolve(new Blob(parts, { type: recorder.mimeType }));
        recorder.onerror = () => reject(new Error('O navegador interrompeu a gravação.'));
      });
      const ended = new Promise((resolve, reject) => {
        video.onended = resolve;
        video.onerror = () => reject(new Error('O vídeo não pôde ser processado.'));
      });
      render(video, title);
      recorder.start(1000);
      await video.play();
      function update() {
        render(video, title);
        status.textContent = 'Exportando ' + Math.round(video.currentTime / video.duration * 100) + '%.';
        animation = requestAnimationFrame(update);
      }
      animation = requestAnimationFrame(update);
      await Promise.race([ended, finished]);
      if (recorder.state !== 'inactive') recorder.stop();
      const blob = await finished;
      setDownload(blob, recorder.mimeType.startsWith('video/mp4') ? 'mp4' : 'webm');
    } finally {
      cancelAnimationFrame(animation);
      video.pause();
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      stream.getTracks().forEach(track => track.stop());
    }
  }

  format.addEventListener('change', () => {
    const isVideo = format.value === 'video';
    source.value = '';
    source.accept = isVideo ? 'video/mp4,video/webm,video/quicktime' : 'image/png,image/jpeg,image/webp';
    root.querySelector('[data-file-label]').textContent = isVideo ? 'Vídeo original' : 'Imagem original';
    root.querySelector('[data-studio-help]').textContent = isVideo
      ? 'Até 60 segundos e 100 MB. Mantenha a aba aberta durante a exportação.'
      : 'Escolha uma imagem de até 20 MB.';
    download.hidden = true;
  });

  const initialFormat = new URLSearchParams(window.location.search).get('formato');
  if (['feed', 'story', 'video'].includes(initialFormat)) {
    format.value = initialFormat;
    format.dispatchEvent(new Event('change'));
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const file = source.files[0];
    const title = form.elements.headline.value.trim();
    const isVideo = format.value === 'video';
    if (!title || !file) return;
    if (file.size > (isVideo ? 100 : 20) * 1024 * 1024) {
      status.textContent = 'O arquivo passou do limite deste formato.';
      return;
    }
    const fields = [...form.querySelectorAll('button, input, select, textarea')];
    for (const field of fields) field.disabled = true;
    download.hidden = true;
    status.textContent = 'Preparando arquivo…';
    canvas.height = format.value === 'feed' ? 1350 : 1920;
    canvas.hidden = false;
    const inputURL = URL.createObjectURL(file);
    try {
      if (isVideo) {
        const Audio = window.AudioContext || window.webkitAudioContext;
        if (Audio) {
          if (!audioContext) {
            audioContext = new Audio();
            audioSource = audioContext.createMediaElementSource(video);
            audioOutput = audioContext.createMediaStreamDestination();
            audioSource.connect(audioOutput);
          }
          await audioContext.resume();
        }
        await renderVideo(inputURL, title);
      } else {
        const image = new Image();
        image.src = inputURL;
        await image.decode();
        render(image, title);
        setDownload(await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .92)), 'jpg');
      }
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : 'Não foi possível gerar o arquivo.';
    } finally {
      video.removeAttribute('src');
      video.load();
      URL.revokeObjectURL(inputURL);
      for (const field of fields) field.disabled = false;
    }
  });
})();

