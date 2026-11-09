// Colour helpers for the dark UI, shared by the board, list and sprints pages.
const DARK_SURFACE = [19, 19, 32]; // #131320, the column/table background

function _hexToRgb(hex){
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((hex || '').trim());
  if(!m) return null;
  let h = m[1];
  if(h.length === 3) h = h.split('').map(c => c + c).join('');
  return [0,2,4].map(i => parseInt(h.slice(i, i+2), 16));
}

function _luminance(c){
  const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}

// A project/column colour made visible on the dark surface: mix it toward white
// until it reaches minRatio contrast. 4.5 (default) is for text, 3 for borders,
// dots and stripes. Colours that already pass are returned unchanged.
function readableOnDark(hex, minRatio = 4.5){
  const rgb = _hexToRgb(hex);
  if(!rgb) return '#f4f6f8';
  const bg = _luminance(DARK_SURFACE) + 0.05;
  let out = rgb;
  for(let t = 0; t <= 1.0001; t += 0.05){
    out = rgb.map(c => Math.round(c + (255 - c) * t));
    if((_luminance(out) + 0.05) / bg >= minRatio) break;
  }
  return `rgb(${out[0]},${out[1]},${out[2]})`;
}

// Black or white text, whichever contrasts more with a coloured pill.
function pillTextColor(hex){
  const rgb = _hexToRgb(hex);
  if(!rgb) return '#fff';
  return _luminance(rgb) > 0.18 ? '#111' : '#fff';
}
