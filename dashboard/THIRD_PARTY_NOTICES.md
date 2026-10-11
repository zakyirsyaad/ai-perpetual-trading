# Third-party notices

The code in this directory is adapted from **jev-trade** by aowang
(<https://github.com/aowang-ai/jev-trade>, MIT License), which is itself based on
**jev-trader** by Jarrod Watts (<https://github.com/jarrodwatts/jev-trader>, MIT License).

Changes made here: the wire types now describe this bot's feed (`confidence` and `reason`
on a decision), the decision panel shows the model's confidence and reason instead of a
probability per action, venue-specific branding, links, token art, and public-site
metadata were removed, and the page is marked noindex. The chart, feed, book and state
handling are otherwise the upstream code.

The MIT License requires that the copyright notices and permission text below travel
with copies of the software, so they are reproduced in full.

---

## jev-trade

MIT License

Copyright (c) 2026 aowang

This repository is based on jev-trader by Jarrod Watts
(https://github.com/jarrodwatts/jev-trader), used under the MIT License.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## jev-trader

MIT License

Copyright (c) 2026 Jarrod Watts

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Packages installed from npm

`next`, `react`, `react-dom` and `lightweight-charts` are installed by `bun install` from the
npm registry under their own licenses (see `bun.lock` for exact versions); they are not copied
into this repository.
