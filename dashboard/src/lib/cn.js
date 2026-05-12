/**
 * cn — 간단한 className 합성 유틸
 * clsx / tailwind-merge 없이 문자열 합치기만 수행.
 * falsy 값(false, null, undefined, 0, "")은 걸러낸다.
 */
export function cn(...classes) {
  return classes
    .flat()
    .filter(Boolean)
    .join(" ")
    .trim();
}
