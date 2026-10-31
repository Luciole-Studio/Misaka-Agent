# sha256 values of 64 zeros are placeholders. The release workflow rewrites them
# from the built archives and publishes this file to the Homebrew tap.
class Misaka < Formula
  desc "A research team of AI agents for the humanities and social sciences"
  homepage "https://github.com/Luciole-Studio/Misaka-Agent"
  version "0.19.21"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/Luciole-Studio/Misaka-Agent/releases/download/v0.19.21/misaka-0.19.21-darwin-arm64.tar.gz"
      sha256 "0000000000000000000000000000000000000000000000000000000000000000"
    end

    on_intel do
      url "https://github.com/Luciole-Studio/Misaka-Agent/releases/download/v0.19.21/misaka-0.19.21-darwin-x86_64.tar.gz"
      sha256 "0000000000000000000000000000000000000000000000000000000000000000"
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/Luciole-Studio/Misaka-Agent/releases/download/v0.19.21/misaka-0.19.21-linux-arm64.tar.gz"
      sha256 "0000000000000000000000000000000000000000000000000000000000000000"
    end

    on_intel do
      url "https://github.com/Luciole-Studio/Misaka-Agent/releases/download/v0.19.21/misaka-0.19.21-linux-x86_64.tar.gz"
      sha256 "0000000000000000000000000000000000000000000000000000000000000000"
    end
  end

  def install
    libexec.install Dir["*"]
    bin.install_symlink libexec/"bin/misaka"
  end

  def caveats
    <<~EOS
      uv, git, ripgrep (rg), fd and poppler (pdftotext) ship inside the bundle.
      Running misaka puts them on PATH for that process. They are not linked into Homebrew's bin.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/misaka --version")
  end
end
