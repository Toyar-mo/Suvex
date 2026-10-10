import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  /*
    ★ `.pytest_cache` 必须显式忽略（2026-10 补）。

    它不该出现在前端仓库里 —— 是 9/25 某个 cwd 落在本目录的 pytest 跑出来的残渣，
    而且 ACL 坏掉了：连 `icacls` 都读不到权限，`Get-ChildItem` 直接 Access denied。

    ESLint 9 起不吃 `.gitignore`，默认递归遍历整棵树，走到这个目录就
    抛 `EPERM: scandir` 并**整体退出**（exit=2）—— 表现为"lint 完全跑不起来"，
    而不是"少查了几个文件"。把它排掉，lint 才能覆盖真正要查的 src/。
    目录本身删不掉（无权限），故这里是忽略而非清理。
  */
  { ignores: ['dist', 'node_modules', '.pytest_cache'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended, prettier],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
  {
    // ReactBits 的拷贝件：为了保持与上游零 diff、便于日后同步，不套我们的规则
    files: ['src/components/effects/**/*.tsx'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      'react-hooks/exhaustive-deps': 'off',
      'react-refresh/only-export-components': 'off',
    },
  },
)
