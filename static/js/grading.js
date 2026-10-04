// Notation d'une copie à partir du corrigé (QR déchiffré ou QCM local) et de la lecture OMR.

/** marks[q][o] : booléen coché ; key : voir crypto.keyFromExam */
export function grade(key, marks) {
  let total = 0, max = 0;
  const details = key.questions.map((q, i) => {
    const m = (marks[i] || []).slice(0, q.nOpt);
    const nCorrect = q.correct.filter(Boolean).length;
    const nWrong = q.nOpt - nCorrect;
    let good = 0, bad = 0;
    q.correct.forEach((c, o) => { if (m[o]) c ? good++ : bad++; });
    let pts = 0;
    if (key.mode === 'partial') {
      if (good + bad > 0) pts = Math.max(0, good / Math.max(nCorrect, 1) - bad / Math.max(nWrong, 1)) * q.points;
    } else if (good === nCorrect && bad === 0) {
      pts = q.points;
    }
    total += pts; max += q.points;
    return { q: i, marked: m.map(Boolean), correct: q.correct, points: round(pts), max: q.points };
  });
  const note = key.noteSur && max ? round((total / max) * key.noteSur) : null;
  return { total: round(total), max: round(max), note, noteSur: key.noteSur || null, details };
}

const round = (x) => Math.round(x * 100) / 100;
