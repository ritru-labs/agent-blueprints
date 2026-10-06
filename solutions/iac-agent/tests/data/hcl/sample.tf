# Sample of agent output, used by tests/test_real_tools.py with the real pinned tools.
resource "aws_vpc" "vpc_main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  tags = {
    Name = "main"
  }
}

resource "aws_subnet" "subnet_app" {
  vpc_id            = aws_vpc.vpc_main.id
  cidr_block        = "10.42.1.0/24"
  availability_zone = "eu-west-1a"
}

resource "aws_security_group" "security_group_web" {
  name        = "web"
  description = "Web tier"
  vpc_id      = aws_vpc.vpc_main.id
}

resource "aws_vpc_security_group_ingress_rule" "vpc_security_group_ingress_rule_0e1" {
  security_group_id = aws_security_group.security_group_web.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_s3_bucket" "s3_bucket_app" {
  bucket = "iacfx-sample-111122223333"
}

resource "aws_s3_bucket_versioning" "s3_bucket_versioning_app" {
  bucket = aws_s3_bucket.s3_bucket_app.id
  versioning_configuration {
    status = "Enabled"
  }
}
