import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** 合并 Tailwind 类名：后面的同类工具类覆盖前面的，冲突交给 tailwind-merge。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
