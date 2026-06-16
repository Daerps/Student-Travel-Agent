## git 项目管理

### 常见命令

git clone(ssh或者http)
git config --global -l 查看
git status 查看**当前工作目录和暂存区的状态**
git add . 将项目所有内容添加到暂存区
git add 文件夹路径/
git add 文件名路径
git restore --staged . 撤回add暂存操作 --staged 表示只针对暂存区操作
git commit -m "说明" 提交到本地仓库

git branch 查看分支名 推荐 git branch -vv 更详细
git push -u origin 分支名 推送并将本地分支与远程分支跟踪关联
git branch -u origin/远程分支名 专门用于只设置跟踪关系
git branch -r 查看远程分支。 查看的是你本地缓存的远程分支信息。如果远程仓库新增了分支，你的本地列表可能不会立即更新。先执行一次 git fetch origin来同步最新的远程信息
git branch -a 同时查看所有分支，本地加远程
git remote show origin 查看远程仓库详细信息

git fetch 仅下载远程代码到本地（不合并）
git pull 下载远程代码 + 自动合并到当前分支

git diff **工作区 vs 暂存区**
git diff --staged **暂存区 vs 本地仓库**
git diff HEAD **“所有还没提交”的改动（工作区和暂存区 vs 本地仓库）**
git log --oneline -10 git log：调出 Git 的提交(commit)历史档案。--oneline：让每条提交记录只占一行，极度精简。-10：限制输出数量，只显示最近的 10 条。

git fetch origin 特定分支
git fetch --all
get log origin/master **当前工作区（正在写的代码）与远程仓库最新代码（origin/master）之间的差异**
git diff origin/master
git merge origin/特定分支 这里合并的是fetch到本地的关于远程特定分支的内容

git merge 和git rebase的区别
git merge master 时，Git 会生成一个新的“合并提交（Merge Commit）”，把两个分支的历史交汇在一起。
git rebase master，Git 会把你 feature 分支独有的提交先暂时存起来，把 master 的最新代码拉过来，然后再把你暂存的提交一个个重新应用到最新的 master 后面。提交历史会变成一条完美的直线，没有任何分叉和多余的合并提交。

### 其他终端命令

- rm -rf 文件夹名

### 设置邮箱

git config --global user.name "你的用户名"
git config --global user.email "你的邮箱"
非全局设置则去掉--global即可

### 创建项目

mkdir adv
cd adv
touch README.md
echo "# Trusted360" >> README.md
git init 会创建.git文件夹
git add README.md （从零创建，如果有内容用git add .）
git commit -m "first commit"
git branch -M main 将当前所在的本地分支重命名为main，这里的都是表示本地名称

去远程仓库平台创建一个仓库
git remote add origin https://github.com/Daerps/Student-Travel-Agent.git 使用 HTTPS 地址。给本地仓库添加一个“远程地址”，并给它起个别名叫 origin
或者git remote add origin git@github.com:Daerps/Student-Travel-Agent.git 使用 SSH 地址（推荐，免重复输入密码）
git push -u origin master 即git push -u [远程仓库] [本地分支名]。这里 -u 表示将本地分支与远程分支关联，之后只需 git push ，不需要后缀了。git push推送分支到远程。这样默认上传到的远程分支是和本地分支名称相同的，如果远程没有 main 分支，Git 会自动创建一个同名的远程分支。
或者 git push -u origin master

注意：可以git push origin <本地分支名>:<远程分支名>，即本地分支名和远程分支名可以不同，但在团队协作中，强烈建议保持本地和远程分支同名

### 推送存在的项目到主分支

git remote add origin https://github.com/Daerps/Trusted360.git
git branch -M master
git push -u origin master

### 创建新分支

git checkout main 切换到主分支main，这里是指本地分支中名为main的
git pull origin main 从远程仓库拉取最新代码并合并，这里的main是指远程仓库的分支
git checkout -b <你的新分支名> 创建并切换到新分支
git branch -vv 查看本地分支是否成功关联了远程分支
输出中，如果 feature/user-login 后面跟着 [origin/feature/user-login]，就说明关联成功了。

### push 细节

- git push -u的详细说明
  Git 会为每一个本地分支独立记录它应该跟踪哪个远程分支。它们之间不会互相替换或干扰。即在不同分支中都可以各自使用-u。并且git push -u 命令后，git pull 也会自动绑定，无需任何额外操作。
  Git 的内部配置会同时记住这两条独立的规则：
  本地 master → 跟踪 → 远程 origin/master
  本地 feature/llm → 跟踪 → 远程 origin/feature/llm
  当你切换到不同的分支时，Git 会自动应用对应的规则。所以：
  在 master 分支上，git push 会推送到 origin/master。
  在 feature/llm 分支上，git push 会推送到 origin/feature/llm。
  你可以随时使用 git branch -vv 命令来清晰地查看所有本地分支的跟踪关系
- 如果某些数据之前已经被 Git 跟踪，仅加 .gitignore 不会生效，需要执行“取消跟踪但保留本地文件”（git rm --cached -r 对应目录）。
  比如sandbox 里的 data 或 output 文件夹之前已经被提交过（即已经在 Git 的索引中），修改 .gitignore 后它们不会自动消失。
  不推荐：先移除 sandbox 下所有已追踪的 data 和 output 文件夹（会删除本地文件）
  git rm -r --force sandbox/*/data
  git rm -r --force sandbox/*/output
  使用安全命令（只去索引，不删文件）
  git rm -r --cached sandbox/*/data
  git rm -r --cached sandbox/*/output
  如果是更深层级的目录
  git rm -r --cached sandbox/**/data
  git rm -r --cached sandbox/**/output
  提交更改
  git add .
  git commit -m "Stop tracking sandbox data and output folders"

### 协作者push（分支保护后）

| 步骤    | 命令                                   | 说明                              |
| :------ | :------------------------------------- | :-------------------------------- |
| 1. 准备 | `git checkout master<br>``git pull`  | 切换到主分支并拉取最新代码        |
| 2. 开发 | `git checkout -b dev-branch`         | 关键： 创建并切换到自己的分支     |
| 3. 提交 | `git add .<br>``git commit -m "..."` | 提交更改到本地分支                |
| 4. 推送 | `git push origin dev-branch`         | 推送分支到远程（不是推送 master） |
| 5. 合并 | *(在 GitHub 网页操作)*               | 发起 Pull Request，等你合并       |

### 代码审查

git stash

gh pr checkout 1

等你处理完 PR，切回原来的分支时，运行 `git stash pop` 即可恢复刚才的代码。

### 注意

- 设置分支保护规则（Branch Protection Rules），限制协作者必须通过“拉取请求（Pull Request）”并经过你的审核后才能合并到主分支
  默认情况下，协作者被添加后拥有“写入（Write）”权限，可以直接 git push 代码到你的仓库，不需要经过你的审核。
- 在分支(比如feature/llm)直接git pull origin master
  将远程 master 分支的最新代码拉取下来，并自动合并（Merge）到你当前所在的 feature/llm 分支中

### 配置ssh密钥

生成ssh密钥，如果已有可以跳过
ssh-keygen -t ed25519 -C "你的邮箱"
将公钥添加到Github
复制.ssh/xxx.pub的内容
在 GitHub 上，点击头像 → Settings → SSH and GPG keys → New SSH key，粘贴公钥并保存。

ssh -T git@github.com 如果看到 “Hi 用户名! You’ve successfully authenticated…”，说明配置成功。

### 本地已有代码中途加入远程仓库

当某人本地已有代码（未使用git），需要将修改合并到已有远程仓库时：

#### 方法一：先提交本地，再合并远程（推荐）

```bash
git init
git remote add origin <仓库地址>
git fetch origin

# 此时本地文件还是他自己的，没有被覆盖
git add .
git commit -m “他的本地修改”

# 然后合并远程代码
git merge origin/master
```

**关键原理：**

- `git fetch` 只下载远程数据到本地缓存，**不改变本地文件**
- `git add .` 把当前本地文件加入暂存区
- `git commit` 把暂存区内容打包成本地提交
- `git merge origin/master` 将远程代码合并到本地，如有冲突需手动解决

#### 方法二：克隆后覆盖（更简单）

```bash
git clone <仓库地址>
# 把他修改过的文件复制进去覆盖
git add .
git commit -m “他的修改”
git push
```

**适用场景：** 修改的文件不多，手动复制覆盖更直观，冲突风险最小。

#### 注意事项

- 合并时可能产生冲突，需要手动解决后再 `git add` 和 `git commit`
- 推荐使用 SSH 地址关联远程仓库，避免每次输入密码
- 如果远程仓库有分支保护，需先推送到自己的分支再提 Pull Request
